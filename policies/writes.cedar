// Writes: only the Flight Ops identity, and only for a caller who passed identity verification this session.
// The confirmation turn ("yes" in a later message) is enforced in agent code by a Strands interrupt
// (common.WriteGate): Cedar cannot see turns.
permit (
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"Duffel___book_offer",
    AgentCore::Action::"Duffel___change_order",
    AgentCore::Action::"Duffel___cancel_order",
    AgentCore::Action::"Duffel___add_bag"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:437987386879:gateway/travelconcierge-traveldeskgateway-a4bksxcakf"
)
when {
  principal.id == "arn:aws:sts::437987386879:assumed-role/TravelConcierge-FlightOpsGateway" &&
  context.input.guest_verified == true
};
