// Reads: both agent identities may call the read tools. Order ownership is checked by the Lambda (Cedar cannot
// see Duffel data). Anything no policy permits is denied (AgentCore Policy is default deny).
permit (
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"Duffel___search_flights",
    AgentCore::Action::"Duffel___get_order",
    AgentCore::Action::"Duffel___list_orders",
    AgentCore::Action::"Duffel___seat_map",
    AgentCore::Action::"Duffel___change_options",
    AgentCore::Action::"Duffel___bag_options"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:437987386879:gateway/travelconcierge-traveldeskgateway-a4bksxcakf"
)
when {
  principal.id == "arn:aws:sts::437987386879:assumed-role/TravelConcierge-DeskGateway" ||
  principal.id == "arn:aws:sts::437987386879:assumed-role/TravelConcierge-FlightOpsGateway"
};
