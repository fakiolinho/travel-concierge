// The Disruption identity (Disruption agent and the read only Flight Ops it calls) may read one order and price
// date changes, nothing else on Duffel. Ownership of the order is still checked by the Lambda.
permit (
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"Duffel___get_order",
    AgentCore::Action::"Duffel___change_options"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:437987386879:gateway/travelconcierge-traveldeskgateway-a4bksxcakf"
)
when {
  principal.id == "arn:aws:sts::437987386879:assumed-role/TravelConcierge-DisruptionGateway"
};
