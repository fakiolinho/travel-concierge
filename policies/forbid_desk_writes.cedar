// Belt and braces: even if a broader permit is added later, the desk identity can never write.
forbid (
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"Duffel___change_order",
    AgentCore::Action::"Duffel___cancel_order",
    AgentCore::Action::"Duffel___add_bag"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:437987386879:gateway/travelconcierge-traveldeskgateway-a4bksxcakf"
)
when {
  principal.id == "arn:aws:sts::437987386879:assumed-role/TravelConcierge-DeskGateway"
};
