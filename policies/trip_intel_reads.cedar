// TripIntel (public weather, holidays, FX; read only, no traveler data): any of the three agent identities.
// Deployed after the TripIntel target exists: the policy engine validates actions against the Gateway's tools.
permit (
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"TripIntel___airport_weather",
    AgentCore::Action::"TripIntel___airport_forecast",
    AgentCore::Action::"TripIntel___destination_forecast",
    AgentCore::Action::"TripIntel___public_holidays",
    AgentCore::Action::"TripIntel___fx_convert"
  ],
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:437987386879:gateway/travelconcierge-traveldeskgateway-a4bksxcakf"
)
when {
  principal.id == "arn:aws:sts::437987386879:assumed-role/TravelConcierge-DeskGateway" ||
  principal.id == "arn:aws:sts::437987386879:assumed-role/TravelConcierge-FlightOpsGateway" ||
  principal.id == "arn:aws:sts::437987386879:assumed-role/TravelConcierge-DisruptionGateway"
};
