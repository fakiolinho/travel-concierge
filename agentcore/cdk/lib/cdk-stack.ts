import {
  AgentCoreApplication,
  AgentCoreMcp,
  AgentCorePaymentManager,
  AgentCorePaymentConnector,
  type AgentCoreProjectSpec,
  type AgentCoreMcpSpec,
  type CustomJWTAuthorizerConfig,
  type HarnessDeploymentConfig,
} from '@aws/agentcore-cdk';
import { CfnOutput, Duration, Stack, type StackProps } from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

/**
 * Harness deployment config: role-scoped fields (for IAM role + container build)
 * plus the full validated spec + its config directory so the L3 construct can
 * synthesize the AWS::BedrockAgentCore::Harness resource.
 */
export type HarnessConfig = HarnessDeploymentConfig;

export interface ManualPaymentConnectorSpec {
  name: string;
  provider: 'CoinbaseCDP' | 'StripePrivy';
  provisionMode?: 'MANUAL';
  credentialName: string;
  credentialProviderArn: string;
}

export interface QuickCreatePaymentConnectorSpec {
  name: string;
  provider: 'CoinbaseCDP';
  provisionMode: 'QUICK_CREATE';
  credentialName?: never;
  credentialProviderArn?: never;
}

export type PaymentConnectorSpec = ManualPaymentConnectorSpec | QuickCreatePaymentConnectorSpec;

export interface PaymentSpec {
  name: string;
  description?: string;
  authorizerType: 'AWS_IAM' | 'CUSTOM_JWT';
  authorizerConfiguration?: { customJWTAuthorizer: CustomJWTAuthorizerConfig };
  autoPayment?: boolean;
  paymentToolAllowlist?: string[];
  networkPreferences?: string[];
  connectors: PaymentConnectorSpec[];
}

export interface AgentCoreStackProps extends StackProps {
  /**
   * The AgentCore project specification containing agents, memories, and credentials.
   */
  spec: AgentCoreProjectSpec;
  /**
   * The MCP specification containing gateways and servers.
   */
  mcpSpec?: AgentCoreMcpSpec;
  /**
   * Credential provider ARNs from deployed state, keyed by credential name.
   */
  credentials?: Record<string, { credentialProviderArn: string; clientSecretArn?: string }>;
  /**
   * Harness role configurations.
   */
  harnesses?: HarnessConfig[];
  /**
   * Parsed connectorParameters for non-S3 KB data sources, keyed by
   * connectorConfigFile path. Forwarded to AgentCoreApplication.
   */
  connectorParametersByFile?: Record<string, Record<string, unknown>>;
  /**
   * Payment specifications with resolved credential provider ARNs.
   */
  paymentSpec?: PaymentSpec[];
}

function toCdkId(name: string): string {
  return name.replace(/_/g, '');
}

/**
 * Decide whether a deployed runtime should receive payment env vars + IAM grants.
 * Payments today only ships a runtime shim for Python HTTP runtimes; injecting
 * AGENTCORE_PAYMENT_* env vars into TypeScript / MCP / A2A / AGUI runtimes
 * would surface env vars they cannot consume and would dilute least-privilege
 * IAM grants for runtimes that never call ProcessPayment.
 */
function isPaymentEligibleAgent(agent: { entrypoint?: string; protocol?: string }): boolean {
  if (agent.protocol && agent.protocol !== 'HTTP') {
    return false;
  }
  const entrypoint = typeof agent.entrypoint === 'string' ? agent.entrypoint : '';
  const entrypointFile = entrypoint.split(':')[0] ?? '';
  return entrypointFile.endsWith('.py');
}

/**
 * CDK Stack that deploys AgentCore infrastructure.
 *
 * This is a thin wrapper that instantiates L3 constructs.
 * All resource logic and outputs are contained within the L3 constructs.
 */
export class AgentCoreStack extends Stack {
  /** The AgentCore application containing all agent environments */
  public readonly application: AgentCoreApplication;

  constructor(scope: Construct, id: string, props: AgentCoreStackProps) {
    super(scope, id, props);

    const { spec, mcpSpec, credentials, harnesses, connectorParametersByFile, paymentSpec } = props;

    // Create AgentCoreApplication with all agents and harness roles
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const appProps: Record<string, unknown> = { spec };
    if (harnesses?.length) {
      appProps.harnesses = harnesses;
    }
    if (connectorParametersByFile && Object.keys(connectorParametersByFile).length > 0) {
      appProps.connectorParametersByFile = connectorParametersByFile;
    }
    if (credentials) {
      appProps.credentials = credentials;
    }
    this.application = new AgentCoreApplication(this, 'Application', appProps as any);

    // Create AgentCoreMcp if there are gateways configured
    if (mcpSpec?.agentCoreGateways && mcpSpec.agentCoreGateways.length > 0) {
      new AgentCoreMcp(this, 'Mcp', {
        projectName: spec.name,
        mcpSpec,
        agentCoreApplication: this.application,
        credentials,
        projectTags: spec.tags,
      });
    }

    // Travel Concierge: one Gateway identity per agent, so Cedar can tell the desk (reads) from Flight Ops
    // (the only principal allowed to write). Fixed role names: policies/*.cedar match the assumed-role ARNs.
    // Trust is the account; who may assume them is decided by the caller's own sts:AssumeRole permission
    // (the deployed desk runtime role below, and admins running the local REPL).
    const gatewayArns = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/*`;
    const gatewayRoles: Record<string, iam.Role> = {};
    for (const [id, roleName] of [
      ['DeskGatewayRole', 'TravelConcierge-DeskGateway'],
      ['FlightOpsGatewayRole', 'TravelConcierge-FlightOpsGateway'],
      // Disruption agent and its read only Flight Ops: TripIntel reads plus get_order/change_options, never writes.
      ['DisruptionGatewayRole', 'TravelConcierge-DisruptionGateway'],
    ] as const) {
      const role = new iam.Role(this, id, {
        roleName,
        assumedBy: new iam.AccountRootPrincipal(),
        maxSessionDuration: Duration.hours(1),
        description: 'AgentCore Gateway caller identity for the Travel Concierge',
      });
      role.addToPolicy(new iam.PolicyStatement({ actions: ['bedrock-agentcore:InvokeGateway'], resources: [gatewayArns] }));
      gatewayRoles[id] = role;
      new CfnOutput(this, `${id}Arn`, { value: role.roleArn });
    }
    for (const env of this.application.environments.values()) {
      env.runtime.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: ['sts:AssumeRole'],
          resources: Object.values(gatewayRoles).map(r => r.roleArn),
        })
      );
    }

    // Travel Concierge: Bedrock Guardrail on both agents' models (Layer A in PLAN.md). Card and passport numbers are
    // blocked on input (the desk never needs them) and masked on output; visa and medical advice are denied topics.
    // ponytail: no contextual grounding check yet. Upgrade: grounding on the policy rules once they live in a KB.
    const blocked =
      "I can't take card or passport numbers, or advise on visas or medical questions, on this line. " +
      'Please leave those out and I will help with your booking.';
    const guardrail = new bedrock.CfnGuardrail(this, 'TravelDeskGuardrail', {
      name: 'TravelConcierge-TravelDesk',
      description: 'PII (card, passport) and denied topics (visa, medical) for the Travel Desk agents',
      blockedInputMessaging: blocked,
      blockedOutputsMessaging: blocked,
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: [
          'CREDIT_DEBIT_CARD_NUMBER',
          'CREDIT_DEBIT_CARD_CVV',
          'CREDIT_DEBIT_CARD_EXPIRY',
          'US_PASSPORT_NUMBER',
        ].map(type => ({ type, action: 'BLOCK', inputAction: 'BLOCK', outputAction: 'ANONYMIZE' })),
        regexesConfig: [
          {
            // Non US passports: "passport", then up to 2 letters and 6 to 9 digits. Digits required: the old
            // [A-Z0-9]{6,9} matched any word ("my passport expires", "passport numbers, or visa/medical").
            name: 'passport_number',
            description: 'Passport number mentioned with the word passport',
            pattern: '(?i)passport\\D{0,20}\\b[A-Z]{0,2}\\d{6,9}\\b',
            action: 'BLOCK',
            inputAction: 'BLOCK',
            outputAction: 'ANONYMIZE',
          },
        ],
      },
      topicPolicyConfig: {
        topicsConfig: [
          {
            name: 'Visa_and_immigration',
            type: 'DENY',
            definition:
              'Advice on visas, entry requirements, immigration status, residence permits or whether a traveler may enter a country.',
            examples: ['Do I need a visa for the UK?', 'Can I enter the US with an expired ESTA?'],
          },
          {
            name: 'Medical_advice',
            type: 'DENY',
            definition:
              'Medical or health advice, including whether a traveler is fit to fly, medication, vaccinations or symptoms.',
            examples: ['Is it safe to fly with a chest infection?', 'Which vaccines do I need for Thailand?'],
          },
        ],
      },
    });
    const guardrailVersion = new bedrock.CfnGuardrailVersion(this, 'TravelDeskGuardrailVersion', {
      guardrailIdentifier: guardrail.attrGuardrailId,
      description: 'Pinned for the agents (v2: passport regex needs digits); edit this to publish a new version',
    });
    // Deployed desk runtime: apply the guardrail, and read/write the traveler Memory (records + conversation events).
    for (const env of this.application.environments.values()) {
      env.runtime.role.addToPrincipalPolicy(
        new iam.PolicyStatement({ actions: ['bedrock:ApplyGuardrail'], resources: [guardrail.attrGuardrailArn] })
      );
      env.runtime.role.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: [
            'bedrock-agentcore:CreateEvent',
            'bedrock-agentcore:GetEvent',
            'bedrock-agentcore:ListEvents',
            'bedrock-agentcore:ListSessions',
            'bedrock-agentcore:ListMemoryRecords',
            'bedrock-agentcore:RetrieveMemoryRecords',
            'bedrock-agentcore:GetMemoryRecord',
          ],
          resources: [`arn:aws:bedrock-agentcore:${this.region}:${this.account}:memory/*`],
        })
      );
    }
    new CfnOutput(this, 'GuardrailIdOutput', { value: guardrail.attrGuardrailId });
    new CfnOutput(this, 'GuardrailVersionOutput', { value: guardrailVersion.attrVersion });

    // Travel Concierge: evals and guardrails dashboard (PLAN.md step 6). Offline eval runs publish to
    // TravelConcierge/Evals (evals/run_offline.py --cloudwatch); online evaluation results land in
    // /aws/bedrock-agentcore/evaluations/results/*. SEARCH expressions keep the widgets free of resource ids.
    const search = (expr: string, stat: string, period: number, label: string) =>
      new cloudwatch.MathExpression({
        expression: `SEARCH('${expr}', '${stat}', ${period})`,
        label,
        period: Duration.seconds(period),
      });
    const day = 86400;
    new cloudwatch.Dashboard(this, 'TravelConciergeEvalsDashboard', {
      dashboardName: 'TravelConcierge-Evals',
      defaultInterval: Duration.days(14),
      widgets: [
        [
          new cloudwatch.TextWidget({
            markdown:
              '## Travel Concierge evals\nOffline runs: `uv run --group evals python evals/run_offline.py --suite dataset ' +
              '--judges --agentcore --decider --cloudwatch`. Online evaluation (`desk_online`, 25% of Runtime sessions) ' +
              'scores live traffic; see Bedrock AgentCore → Evaluations. Traces: CloudWatch → GenAI Observability.',
            width: 24,
            height: 2,
          }),
        ],
        [
          new cloudwatch.GraphWidget({
            title: 'Deterministic gate pass rate per suite (offline)',
            left: [search('{TravelConcierge/Evals,Suite} MetricName="GatePass"', 'Average', day, '')],
            leftYAxis: { min: 0, max: 1 },
            width: 8,
          }),
          new cloudwatch.GraphWidget({
            title: 'Gate pass rate per category (offline)',
            left: [search('{TravelConcierge/Evals,Category,Suite} MetricName="GatePass"', 'Average', day, '')],
            leftYAxis: { min: 0, max: 1 },
            width: 8,
          }),
          new cloudwatch.GraphWidget({
            title: 'Judge scores (Strands Evals + AgentCore, offline)',
            left: [search('{TravelConcierge/Evals,Evaluator,Suite} MetricName="JudgeScore"', 'Average', day, '')],
            leftYAxis: { min: 0, max: 1 },
            width: 8,
          }),
        ],
        [
          new cloudwatch.GraphWidget({
            title: 'Turn latency p50 / p90 per category (offline, s)',
            left: [
              search('{TravelConcierge/Evals,Category,Suite} MetricName="TurnSeconds"', 'p50', day, 'p50'),
              search('{TravelConcierge/Evals,Category,Suite} MetricName="TurnSeconds"', 'p90', day, 'p90'),
            ],
            width: 8,
          }),
          new cloudwatch.GraphWidget({
            title: 'Runtime invocation latency p50 / p90 (ms)',
            left: [
              search('{AWS/Bedrock-AgentCore,Name,Operation,Resource} MetricName="Latency" Operation="InvokeAgentRuntime"', 'p50', 3600, 'p50'),
              search('{AWS/Bedrock-AgentCore,Name,Operation,Resource} MetricName="Latency" Operation="InvokeAgentRuntime"', 'p90', 3600, 'p90'),
            ],
            width: 8,
          }),
          new cloudwatch.GraphWidget({
            title: 'Strands Decider 2B: mean P(yes) per gate question',
            left: [search('{TravelConcierge/Evals,Question,Suite} MetricName="DeciderPYes"', 'Average', day, '')],
            leftYAxis: { min: 0, max: 1 },
            width: 8,
          }),
        ],
        [
          new cloudwatch.GraphWidget({
            title: 'Cedar deny decisions per policy (Gateway)',
            left: [search('{AWS/Bedrock-AgentCore,Mode,OperationName,Policy,TargetResource} MetricName="DenyDecisions"', 'Sum', 3600, '')],
            width: 8,
          }),
          new cloudwatch.GraphWidget({
            title: 'Guardrail interventions (input / output)',
            left: [search('{AWS/Bedrock/Guardrails,GuardrailContentSource,Operation} MetricName="InvocationsIntervened"', 'Sum', 3600, '')],
            width: 8,
          }),
          new cloudwatch.GraphWidget({
            title: 'Errors: Runtime, Gateway, Lambdas',
            left: [
              search('{AWS/Bedrock-AgentCore,Name,Operation,Resource} MetricName="Errors" Operation="InvokeAgentRuntime"', 'Sum', 3600, ''),
              search('{AWS/Bedrock-AgentCore,Method,Operation,Protocol,Resource} MetricName="SystemErrors"', 'Sum', 3600, ''),
              search('{AWS/Lambda,FunctionName} MetricName="Errors" FunctionName="TravelConcierge-"', 'Sum', 3600, ''),
            ],
            width: 8,
          }),
        ],
        [
          new cloudwatch.LogQueryWidget({
            title: 'Latest evaluation results (Strands Evals offline runs)',
            logGroupNames: ['/aws/bedrock-agentcore/evaluations/results/travel-concierge'],
            queryLines: [
              'fields @timestamp, `gen_ai.evaluation.name`, `gen_ai.evaluation.score.value`, `gen_ai.evaluation.score.label`, `session.id`',
              'sort @timestamp desc',
              'limit 50',
            ],
            width: 24,
            height: 6,
          }),
        ],
      ],
    });

    // Create payment infrastructure via CFN constructs
    if (paymentSpec && paymentSpec.length > 0) {
      for (const payment of paymentSpec) {
        const mgrId = toCdkId(payment.name);
        const manager = new AgentCorePaymentManager(this, `Payment${mgrId}`, {
          projectName: spec.name,
          name: payment.name,
          authorizerType: payment.authorizerType,
          description: payment.description,
          authorizerConfiguration: payment.authorizerConfiguration,
          tags: spec.tags,
        });

        const prefix = `AGENTCORE_PAYMENT_${payment.name.toUpperCase().replace(/-/g, '_')}`;

        // Wire env vars from construct output tokens into eligible agent environments only.
        // See isPaymentEligibleAgent — non-Python or non-HTTP runtimes have no shim that
        // can consume these env vars, and giving them sts:AssumeRole on the
        // ProcessPaymentRole would broaden the privilege surface unnecessarily.
        for (const env of this.application.environments.values()) {
          if (!isPaymentEligibleAgent(env.agent)) {
            continue;
          }
          env.runtime.addEnvironmentVariable(`${prefix}_MANAGER_ARN`, manager.paymentManagerArn);
          env.runtime.addEnvironmentVariable(`${prefix}_PROCESS_PAYMENT_ROLE_ARN`, manager.processPaymentRoleArn);

          // Grant runtime execution role permission to assume the ProcessPaymentRole.
          // The ProcessPaymentRole's trust policy allows AccountRootPrincipal, but the
          // caller still needs sts:AssumeRole on its own role to perform the assumption.
          env.runtime.role.addToPrincipalPolicy(
            new iam.PolicyStatement({
              actions: ['sts:AssumeRole'],
              resources: [manager.processPaymentRoleArn],
            })
          );

          // Grant payment data-plane actions directly to the runtime role.
          //
          // NOTE: This deviates from the canonical role model in the AgentCore Payments
          // beta guide, which assigns Get/List/Create instrument+session actions to a
          // separate ManagementRole and limits the agent's role to ProcessPayment only.
          // The current SDK plugin (AgentCorePaymentsPlugin.generate_payment_header)
          // calls GetPaymentInstrument internally during the 402 auto-pay path, so the
          // runtime role needs read access. CreatePaymentSession is included so
          // `agentcore invoke --auto-session` works without a separate ManagementRole
          // call. Tighten this if the SDK is updated to accept pre-fetched instrument
          // details and split create-session into a backend-only flow.
          env.runtime.role.addToPrincipalPolicy(
            new iam.PolicyStatement({
              actions: [
                'bedrock-agentcore:GetPaymentInstrument',
                'bedrock-agentcore:ListPaymentInstruments',
                'bedrock-agentcore:GetPaymentInstrumentBalance',
                'bedrock-agentcore:GetPaymentSession',
                'bedrock-agentcore:ListPaymentSessions',
                'bedrock-agentcore:CreatePaymentSession',
                'bedrock-agentcore:ProcessPayment',
              ],
              resources: [manager.paymentManagerArn, `${manager.paymentManagerArn}/*`],
            })
          );

          if (payment.autoPayment !== undefined) {
            env.runtime.addEnvironmentVariable(`${prefix}_AUTO_PAYMENT`, String(payment.autoPayment));
          }
          if (payment.paymentToolAllowlist) {
            env.runtime.addEnvironmentVariable(`${prefix}_TOOL_ALLOWLIST`, payment.paymentToolAllowlist.join(','));
          }
          if (payment.networkPreferences) {
            env.runtime.addEnvironmentVariable(`${prefix}_NETWORK_PREFERENCES`, payment.networkPreferences.join(','));
          }
          if (payment.authorizerType === 'CUSTOM_JWT') {
            env.runtime.addEnvironmentVariable(`${prefix}_AUTH_MODE`, 'bearer');
          }
        }

        // Create connectors for this manager
        for (const connector of payment.connectors) {
          const connId = toCdkId(connector.name);
          const schemaConnector =
            connector.provisionMode === 'QUICK_CREATE'
              ? connector
              : {
                  name: connector.name,
                  provider: connector.provider,
                  ...(connector.provisionMode && { provisionMode: connector.provisionMode }),
                  credentialName: connector.credentialName,
                };
          const compatibilityProps = {
            projectName: spec.name,
            paymentManager: manager,
            connector: schemaConnector,
            // Remove these legacy manual fields after the new L3 release is pinned.
            connectorName: connector.name,
            connectorType: connector.provider,
            ...(connector.provisionMode !== 'QUICK_CREATE' && {
              credentialProviderArn: connector.credentialProviderArn,
            }),
          };
          const conn = new AgentCorePaymentConnector(
            this,
            `Payment${mgrId}${connId}`,
            compatibilityProps as unknown as ConstructorParameters<typeof AgentCorePaymentConnector>[2]
          );

          // Wire first connector's ID as env var (eligible agents only)
          if (connector === payment.connectors[0]) {
            for (const env of this.application.environments.values()) {
              if (!isPaymentEligibleAgent(env.agent)) continue;
              env.runtime.addEnvironmentVariable(`${prefix}_CONNECTOR_ID`, conn.paymentConnectorId);
            }
          }

          new CfnOutput(this, `Payment${mgrId}${connId}ConnectorId`, {
            value: conn.paymentConnectorId,
          });
          if (connector.provisionMode === 'QUICK_CREATE') {
            const quickCreateConnector = conn as AgentCorePaymentConnector & {
              paymentConnectorStatus: string;
              authorizationUrl: string;
            };
            new CfnOutput(this, `Payment${mgrId}${connId}ConnectorStatus`, {
              value: quickCreateConnector.paymentConnectorStatus,
            });
            new CfnOutput(this, `Payment${mgrId}${connId}AuthorizationUrl`, {
              value: quickCreateConnector.authorizationUrl,
            });
          }
        }

        // CFN Outputs for post-deploy state parsing
        new CfnOutput(this, `Payment${mgrId}ManagerArn`, {
          value: manager.paymentManagerArn,
        });
        new CfnOutput(this, `Payment${mgrId}ManagerId`, {
          value: manager.paymentManagerId,
        });
        new CfnOutput(this, `Payment${mgrId}ProcessPaymentRoleArn`, {
          value: manager.processPaymentRoleArn,
        });
        new CfnOutput(this, `Payment${mgrId}ResourceRetrievalRoleArn`, {
          value: manager.resourceRetrievalRoleArn,
        });
      }
    }

    // Stack-level output
    new CfnOutput(this, 'StackNameOutput', {
      description: 'Name of the CloudFormation Stack',
      value: this.stackName,
    });
  }
}
