import React from 'react';
import { 
  Plus, 
  Minus, 
  Truck, 
  Edit3, 
  Trash2 
} from 'lucide-react';
import { playClickSound, playStockChime } from '../utils/audio';

export default function InventoryTable({
  items,
  recentlyUpdatedId,
  onAdjustStock,
  onOpenEditModal,
  onDeleteItem,
  onQuickRestock
}) {
  if (items.length === 0) {
    return (
      <div style={{
        textAlign: 'center',
        padding: '3rem',
        background: 'var(--bg-card)',
        borderRadius: 'var(--radius-lg)',
        color: 'var(--text-dim)'
      }}>
        No inventory items match current filter criteria.
      </div>
    );
  }

  return (
    <div className="table-container" id="inventory-table-container">
      <table className="nexus-table" id="inventory-data-table">
        <thead>
          <tr>
            <th>SKU</th>
            <th>Item Name &amp; Supplier</th>
            <th>Category</th>
            <th>Warehouse Location</th>
            <th style={{ textAlign: 'right' }}>Stock Level</th>
            <th style={{ textAlign: 'center' }}>Status</th>
            <th style={{ textAlign: 'right' }}>Unit Cost</th>
            <th style={{ textAlign: 'right' }}>Valuation</th>
            <th style={{ textAlign: 'right' }}>Quick Adjust</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const isLow = item.quantity > 0 && item.quantity <= item.reorderLevel;
            const isDepleted = item.quantity === 0;
            const statusClass = isDepleted 
              ? 'badge-out-of-stock' 
              : isLow 
              ? 'badge-low-stock' 
              : 'badge-in-stock';
            const statusLabel = isDepleted 
              ? 'Depleted' 
              : isLow 
              ? 'Low' 
              : 'Optimal';
            const isUpdated = recentlyUpdatedId === item.id;
            const valuation = item.quantity * item.unitCost;

            return (
              <tr 
                key={item.id} 
                id={`table-row-${item.id}`}
                className={isUpdated ? 'flash-update' : ''}
              >
                <td>
                  <span className="item-sku">{item.sku}</span>
                </td>
                <td>
                  <div style={{ fontWeight: 700, color: 'var(--text-main)', maxWidth: '280px' }}>
                    {item.name}
                  </div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)' }}>
                    Supplier: {item.supplier}
                  </div>
                </td>
                <td>
                  <span style={{ fontSize: '0.8125rem', color: 'var(--text-muted)' }}>
                    {item.category}
                  </span>
                </td>
                <td>
                  <div style={{ fontSize: '0.8125rem', color: 'var(--text-main)' }}>
                    {item.location}
                  </div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)' }}>
                    {item.bin}
                  </div>
                </td>
                <td style={{ textAlign: 'right' }}>
                  <div style={{ fontWeight: 800, fontSize: '1rem', color: isDepleted ? 'var(--rose-glow)' : 'var(--text-main)' }} className="mono-num">
                    {item.quantity} <span style={{ fontSize: '0.75rem', color: 'var(--text-dim)', fontWeight: 500 }}>{item.unit}</span>
                  </div>
                  <div style={{ fontSize: '0.6875rem', color: 'var(--text-dim)', fontFamily: 'var(--font-mono)' }}>
                    Reorder @ {item.reorderLevel}
                  </div>
                </td>
                <td style={{ textAlign: 'center' }}>
                  <span className={`status-badge ${statusClass}`}>
                    {statusLabel}
                  </span>
                </td>
                <td style={{ textAlign: 'right' }} className="mono-num">
                  ${item.unitCost.toFixed(2)}
                </td>
                <td style={{ textAlign: 'right', fontWeight: 700, color: 'var(--emerald-glow)' }} className="mono-num">
                  ${valuation.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </td>
                <td style={{ textAlign: 'right' }}>
                  <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem' }}>
                    <button
                      id={`tbl-dec-${item.id}`}
                      className="adj-btn danger"
                      onClick={() => {
                        playClickSound();
                        onAdjustStock(item.id, -1, "Manual deduction (-1)");
                      }}
                      title="Deduct 1"
                    >
                      <Minus size={12} />
                    </button>
                    <button
                      id={`tbl-inc-${item.id}`}
                      className="adj-btn success"
                      onClick={() => {
                        playClickSound();
                        onAdjustStock(item.id, 1, "Manual increment (+1)");
                      }}
                      title="Add 1"
                    >
                      <Plus size={12} />
                    </button>
                    {isLow && (
                      <button
                        id={`tbl-restock-${item.id}`}
                        className="adj-btn"
                        style={{ background: 'var(--amber-soft)', color: 'var(--amber-glow)', borderColor: 'var(--amber-border)' }}
                        onClick={() => {
                          playStockChime();
                          onQuickRestock(item.id);
                        }}
                        title="Restock to Capacity"
                      >
                        <Truck size={12} />
                      </button>
                    )}
                    <button
                      id={`tbl-edit-${item.id}`}
                      className="adj-btn"
                      onClick={() => {
                        playClickSound();
                        onOpenEditModal(item);
                      }}
                      title="Edit SKU"
                    >
                      <Edit3 size={12} />
                    </button>
                    <button
                      id={`tbl-delete-${item.id}`}
                      className="adj-btn danger"
                      onClick={() => {
                        playClickSound();
                        if (window.confirm(`Delete SKU ${item.sku}?`)) {
                          onDeleteItem(item.id);
                        }
                      }}
                      title="Delete SKU"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
