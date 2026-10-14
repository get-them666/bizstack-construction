// Real-time Event Simulator Engine
export function generateRandomEvent(inventoryItems) {
  if (!inventoryItems || inventoryItems.length === 0) return null;

  // Pick a random item
  const randomIndex = Math.floor(Math.random() * inventoryItems.length);
  const targetItem = inventoryItems[randomIndex];

  const operators = [
    { name: "Marcus Reed", role: "Site Lead" },
    { name: "Elena Rostova", role: "Warehouse Mgr" },
    { name: "Dave K.", role: "Lead Electrician" },
    { name: "Carlos M.", role: "Framing Foreman" },
    { name: "Sarah Jenkins", role: "Procurement Agent" },
    { name: "Automated RFID Gate", role: "Telemetry Sensor #04" }
  ];
  const operator = operators[Math.floor(Math.random() * operators.length)];

  // Determine event type
  // Weight towards outbound/usage (60%), inbound/delivery (30%), cycle count/audit (10%)
  const rand = Math.random();
  let type = "outbound";
  let delta = 0;
  let reason = "";

  if (rand < 0.6) {
    type = "outbound";
    // Check out between 1 and min(8, current stock)
    const maxDeduct = Math.max(1, Math.min(8, targetItem.quantity));
    delta = -Math.floor(Math.random() * maxDeduct + 1);
    const reasons = [
      `Jobsite staging draw for Phase 2`,
      `Immediate crew dispatch consumption`,
      `Service truck replenishment`,
      `Damaged/defective unit write-off`
    ];
    reason = reasons[Math.floor(Math.random() * reasons.length)];
  } else if (rand < 0.9) {
    type = "inbound";
    // Inbound receipt
    delta = Math.floor(Math.random() * 20 + 5);
    const reasons = [
      `PO Freight delivery received`,
      `Supplier direct drop off`,
      `Surplus returned from Jobsite #104`,
      `Express courier restock`
    ];
    reason = reasons[Math.floor(Math.random() * reasons.length)];
  } else {
    type = "audit";
    // Minor cycle count alignment
    delta = Math.random() > 0.5 ? 1 : -1;
    reason = "Physical cycle count discrepancy correction";
  }

  // Ensure stock doesn't go below 0
  const newQuantity = Math.max(0, targetItem.quantity + delta);
  const actualDelta = newQuantity - targetItem.quantity;

  const event = {
    id: `evt-${Date.now()}-${Math.random().toString(36).substr(2, 5)}`,
    timestamp: new Date().toISOString(),
    itemId: targetItem.id,
    itemName: targetItem.name,
    sku: targetItem.sku,
    type,
    delta: actualDelta,
    previousQty: targetItem.quantity,
    newQty: newQuantity,
    unit: targetItem.unit,
    operator: operator.name,
    role: operator.role,
    location: targetItem.location,
    reason
  };

  return {
    targetItemId: targetItem.id,
    newQuantity,
    event
  };
}
