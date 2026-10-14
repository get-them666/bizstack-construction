import React from 'react';
import { 
  MapPin, 
  Plus, 
  Minus, 
  Truck, 
  Edit3, 
  Trash2, 
  Building2 
} from 'lucide-react';
import { playClickSound, playStockChime } from '../utils/audio';

export default function InventoryGrid({
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
        padding: '4rem 2rem',
        background: 'var(--bg-card)',
        borderRadius: 'var(--radius-lg)',
        border: '1px solid var(--border-subtle)',
        color: 'var(--text-dim)'
      }}>
        <p style={{ fontSize: '1.125rem', fontWeight: 600, color: 'var(--text-muted)' }}>
          No inventory items matched your filter criteria.
        </p>
        <p style={{ fontSize: '0.875rem', marginTop: '0.5rem' }}>
          Try clearing search filters or add a new SKU.
        </p>
      </div>
    );
  }

  return (
    <div className="inventory-cards-grid" id="inventory-grid-container">
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
          ? 'Low Stock' 
          : 'Optimal';
        
        const capacityPercent = Math.min(
          100, 
          Math.max(0, Math.round((item.quantity / (item.maxCapacity || 100)) * 100))
        );
        const meterColor = isDepleted 
          ? 'var(--rose-glow)' 
          : isLow 
          ? 'var(--amber-glow)' 
          : 'var(--emerald-glow)';
        
        const isUpdated = recentlyUpdatedId === item.id;

        return (
          <article
            key={item.id}
            id={`item-card-${item.id}`}
            className={`item-card ${isUpdated ? 'flash-update' : ''}`}
          >
            {/* Card Header: SKU & Category */}
            <div className="item-card-header">
              <span className="item-sku">{item.sku}</span>
              <span className={`status-badge ${statusClass}`}>
                {statusLabel}
              </span>
            </div>

            {/* Item Title & Category */}
            <div style={{ marginBottom: '0.5rem' }}>
              <span className="item-category-tag">{item.category}</span>
              <h2 className="item-name" title={item.name}>{item.name}</h2>
            </div>

            {/* Location & Bin */}
            <div className="item-location">
              <MapPin size={13} color="var(--cyan-glow)" />
              <span>{item.location} • <strong style={{ color: 'var(--text-muted)' }}>{item.bin}</strong></span>
            </div>

            {/* Stock Meter & Numbers */}
            <div className="stock-meter-container">
              <div className="stock-meter-header">
                <div>
                  <span className="stock-current mono-num" style={{ color: isDepleted ? 'var(--rose-glow)' : 'inherit' }}>
                    {item.quantity}
                  </span>
                  <span className="stock-unit">{item.unit}</span>
                </div>
                <div className="stock-target">
                  Reorder @ <span style={{ color: 'var(--amber-glow)' }}>{item.reorderLevel}</span> / Max {item.maxCapacity}
                </div>
              </div>

              {/* Progress bar */}
              <div className="meter-track">
                <div
                  className="meter-fill"
                  style={{
                    width: `${capacityPercent}%`,
                    backgroundColor: meterColor
                  }}
                />
              </div>

              {/* Cost & Valuation */}
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '0.5rem', fontSize: '0.75rem', color: 'var(--text-dim)' }}>
                <span>Unit: <strong className="mono-num" style={{ color: 'var(--text-muted)' }}>${item.unitCost.toFixed(2)}</strong></span>
                <span>Valuation: <strong className="mono-num" style={{ color: 'var(--emerald-glow)' }}>${(item.quantity * item.unitCost).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></span>
              </div>
            </div>

            {/* Supplier reference */}
            <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)', display: 'flex', alignItems: 'center', gap: '0.375rem', marginBottom: '0.75rem' }}>
              <Building2 size={13} />
              <span>Supplier: <strong style={{ color: 'var(--text-muted)' }}>{item.supplier}</strong></span>
            </div>

            {/* Actions Bar */}
            <div className="item-card-actions">
              {/* Quick Steppers: -5, -1, +1, +5 */}
              <div className="quick-adj-group">
                <button
                  id={`btn-dec-5-${item.id}`}
                  className="adj-btn danger"
                  onClick={() => {
                    playClickSound();
                    onAdjustStock(item.id, -5, "Manual deduction (-5)");
                  }}
                  title="Deduct 5"
                >
                  -5
                </button>
                <button
                  id={`btn-dec-1-${item.id}`}
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
                  id={`btn-inc-1-${item.id}`}
                  className="adj-btn success"
                  onClick={() => {
                    playClickSound();
                    onAdjustStock(item.id, 1, "Manual increment (+1)");
                  }}
                  title="Add 1"
                >
                  <Plus size={12} />
                </button>
                <button
                  id={`btn-inc-5-${item.id}`}
                  className="adj-btn success"
                  onClick={() => {
                    playClickSound();
                    onAdjustStock(item.id, 5, "Manual increment (+5)");
                  }}
                  title="Add 5"
                >
                  +5
                </button>
              </div>

              {/* Utility buttons */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
                {isLow && (
                  <button
                    id={`btn-restock-${item.id}`}
                    className="adj-btn"
                    style={{ background: 'var(--amber-soft)', color: 'var(--amber-glow)', borderColor: 'var(--amber-border)' }}
                    onClick={() => {
                      playStockChime();
                      onQuickRestock(item.id);
                    }}
                    title="Quick restock to max capacity"
                  >
                    <Truck size={13} />
                  </button>
                )}
                <button
                  id={`btn-edit-${item.id}`}
                  className="adj-btn"
                  onClick={() => {
                    playClickSound();
                    onOpenEditModal(item);
                  }}
                  title="Edit SKU Details"
                >
                  <Edit3 size={13} />
                </button>
                <button
                  id={`btn-delete-${item.id}`}
                  className="adj-btn danger"
                  onClick={() => {
                    playClickSound();
                    if (window.confirm(`Delete SKU ${item.sku} (${item.name})?`)) {
                      onDeleteItem(item.id);
                    }
                  }}
                  title="Delete SKU"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          </article>
        );
      })}
    </div>
  );
}
