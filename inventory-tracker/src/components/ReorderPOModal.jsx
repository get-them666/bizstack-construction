import React from 'react';
import { X, FileText, CheckCheck, Download, AlertTriangle, Building2 } from 'lucide-react';
import { playStockChime } from '../utils/audio';

export default function ReorderPOModal({ isOpen, onClose, items, onExecutePO }) {
  if (!isOpen) return null;

  // Filter low or depleted items
  const lowItems = items.filter((item) => item.quantity <= item.reorderLevel);

  // Group by supplier
  const supplierGroups = lowItems.reduce((acc, item) => {
    const s = item.supplier || 'Unassigned Supplier';
    if (!acc[s]) acc[s] = [];
    const recommendedQty = Math.max(1, (item.maxCapacity || 100) - item.quantity);
    acc[s].push({
      ...item,
      orderQty: recommendedQty,
      lineTotal: recommendedQty * item.unitCost
    });
    return acc;
  }, {});

  const totalPOCost = Object.values(supplierGroups)
    .flat()
    .reduce((sum, item) => sum + item.lineTotal, 0);

  const poNumber = `PO-2026-${Math.floor(1000 + Math.random() * 9000)}`;

  const handleExportCSV = () => {
    const headers = ['PO_Number', 'Supplier', 'SKU', 'Item_Name', 'Order_Quantity', 'Unit', 'Unit_Cost', 'Line_Total'];
    const rows = [];
    Object.entries(supplierGroups).forEach(([supplier, lines]) => {
      lines.forEach((l) => {
        rows.push([
          poNumber,
          `"${supplier}"`,
          l.sku,
          `"${l.name}"`,
          l.orderQty,
          l.unit,
          l.unitCost.toFixed(2),
          l.lineTotal.toFixed(2)
        ]);
      });
    });

    const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement('a');
    link.setAttribute('href', encodedUri);
    link.setAttribute('download', `${poNumber}_Manifest.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleConfirmOrder = () => {
    playStockChime();
    const replenishmentOrders = Object.values(supplierGroups).flat().map((item) => ({
      itemId: item.id,
      addedQty: item.orderQty,
      itemName: item.name
    }));
    onExecutePO(replenishmentOrders, poNumber);
    onClose();
  };

  return (
    <div className="modal-overlay" id="modal-reorder-po" role="dialog" aria-modal="true">
      <div className="modal-card" style={{ maxWidth: '720px' }}>
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <FileText size={20} color="var(--indigo-glow)" />
            <h3 className="modal-title" id="po-modal-title">Automated Replenishment Purchase Order</h3>
          </div>
          <button id="btn-close-po-modal" className="modal-close-btn" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <div className="modal-body">
          {/* PO Summary Header */}
          <div style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            background: 'rgba(0, 0, 0, 0.3)',
            padding: '1rem',
            borderRadius: 'var(--radius-md)',
            border: '1px solid var(--border-subtle)'
          }}>
            <div>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Purchase Order #</div>
              <div style={{ fontSize: '1.25rem', fontWeight: 800, fontFamily: 'var(--font-mono)', color: 'var(--indigo-glow)' }}>
                {poNumber}
              </div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Total Order Valuation</div>
              <div style={{ fontSize: '1.25rem', fontWeight: 800, fontFamily: 'var(--font-mono)', color: 'var(--emerald-glow)' }}>
                ${totalPOCost.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </div>
            </div>
          </div>

          {lowItems.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
              <CheckCheck size={36} color="var(--emerald-glow)" style={{ margin: '0 auto 0.5rem auto' }} />
              <p style={{ fontWeight: 700 }}>All inventory items are currently above reorder thresholds!</p>
              <p style={{ fontSize: '0.8125rem', color: 'var(--text-dim)', marginTop: '0.25rem' }}>
                No emergency replenishment needed at this time.
              </p>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
              {Object.entries(supplierGroups).map(([supplier, lines]) => (
                <div 
                  key={supplier}
                  style={{
                    background: 'rgba(255, 255, 255, 0.02)',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    overflow: 'hidden'
                  }}
                >
                  <div style={{
                    padding: '0.625rem 1rem',
                    background: 'rgba(255, 255, 255, 0.04)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.5rem',
                    fontSize: '0.8125rem',
                    fontWeight: 700,
                    color: 'var(--text-main)'
                  }}>
                    <Building2 size={14} color="var(--cyan-glow)" />
                    <span>Vendor: {supplier}</span>
                    <span style={{ fontSize: '0.75rem', color: 'var(--text-dim)', marginLeft: 'auto' }}>
                      {lines.length} Line Items
                    </span>
                  </div>

                  <div style={{ padding: '0.75rem 1rem' }}>
                    {lines.map((l) => (
                      <div 
                        key={l.id}
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          padding: '0.5rem 0',
                          borderBottom: '1px solid rgba(255, 255, 255, 0.04)',
                          fontSize: '0.8125rem'
                        }}
                      >
                        <div>
                          <div style={{ fontWeight: 600, color: 'var(--text-main)' }}>
                            <span className="item-sku" style={{ marginRight: '0.5rem' }}>{l.sku}</span>
                            {l.name}
                          </div>
                          <div style={{ fontSize: '0.75rem', color: 'var(--amber-glow)', marginTop: '0.125rem' }}>
                            Current: {l.quantity} / Alert At: {l.reorderLevel}
                          </div>
                        </div>

                        <div style={{ textAlign: 'right' }}>
                          <div style={{ fontWeight: 700, color: 'var(--emerald-glow)', fontFamily: 'var(--font-mono)' }}>
                            Order +{l.orderQty} {l.unit}
                          </div>
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)', fontFamily: 'var(--font-mono)' }}>
                            ${l.lineTotal.toFixed(2)}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="modal-footer">
          {lowItems.length > 0 && (
            <>
              <button
                id="btn-export-po-csv"
                className="btn-secondary"
                onClick={handleExportCSV}
              >
                <Download size={14} /> Export PO (CSV)
              </button>
              <button
                id="btn-confirm-po-order"
                className="btn-primary"
                onClick={handleConfirmOrder}
              >
                <CheckCheck size={16} /> Confirm &amp; Receive Replenishment
              </button>
            </>
          )}
          {lowItems.length === 0 && (
            <button id="btn-close-empty-po" className="btn-secondary" onClick={onClose}>
              Close
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
