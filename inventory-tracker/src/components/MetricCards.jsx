import React from 'react';
import { 
  DollarSign, 
  Package, 
  AlertTriangle, 
  XCircle, 
  ShieldCheck 
} from 'lucide-react';
import { playClickSound } from '../utils/audio';

export default function MetricCards({ items, currentFilter, onSelectStatusFilter }) {
  // Calculations
  const totalValue = items.reduce((acc, item) => acc + (item.quantity * item.unitCost), 0);
  const totalSKUs = items.length;
  const lowStockCount = items.filter(i => i.quantity > 0 && i.quantity <= i.reorderLevel).length;
  const outOfStockCount = items.filter(i => i.quantity === 0).length;
  const healthyCount = items.filter(i => i.quantity > i.reorderLevel).length;
  const healthPercent = totalSKUs > 0 ? Math.round((healthyCount / totalSKUs) * 100) : 100;

  return (
    <section className="metrics-grid" id="metrics-overview" aria-label="Key Performance Indicators">
      {/* 1. Total Valuation */}
      <div className="metric-card card-emerald" id="card-valuation">
        <div className="metric-header">
          <span className="metric-label">Total Inventory Valuation</span>
          <div className="metric-icon-wrap" style={{ background: 'var(--emerald-soft)', color: 'var(--emerald-glow)' }}>
            <DollarSign size={18} />
          </div>
        </div>
        <div className="metric-value mono-num" id="metric-total-value">
          ${totalValue.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
        </div>
        <div className="metric-footer">
          <span>Real-time on-hand replacement value</span>
        </div>
      </div>

      {/* 2. Total SKUs */}
      <div className="metric-card card-cyan" id="card-total-skus">
        <div className="metric-header">
          <span className="metric-label">Active SKUs</span>
          <div className="metric-icon-wrap" style={{ background: 'var(--cyan-soft)', color: 'var(--cyan-glow)' }}>
            <Package size={18} />
          </div>
        </div>
        <div className="metric-value mono-num" id="metric-total-skus">
          {totalSKUs}
        </div>
        <div className="metric-footer">
          <span>Across 4 operating warehouses & vans</span>
        </div>
      </div>

      {/* 3. Low Stock Warning */}
      <div 
        className="metric-card card-amber" 
        id="card-low-stock"
        style={{ cursor: 'pointer' }}
        onClick={() => {
          playClickSound();
          onSelectStatusFilter(currentFilter === 'low' ? 'all' : 'low');
        }}
        title="Click to filter low stock items"
      >
        <div className="metric-header">
          <span className="metric-label">Reorder Threshold Alerts</span>
          <div className="metric-icon-wrap" style={{ background: 'var(--amber-soft)', color: 'var(--amber-glow)' }}>
            <AlertTriangle size={18} />
          </div>
        </div>
        <div className="metric-value mono-num" id="metric-low-stock-count" style={{ color: 'var(--amber-glow)' }}>
          {lowStockCount}
        </div>
        <div className="metric-footer">
          <span style={{ color: lowStockCount > 0 ? 'var(--amber-glow)' : 'var(--text-dim)' }}>
            {lowStockCount > 0 ? 'Action needed: At or below reorder level' : 'All buffer stocks intact'}
          </span>
        </div>
      </div>

      {/* 4. Out of Stock Critical */}
      <div 
        className="metric-card card-rose" 
        id="card-out-of-stock"
        style={{ cursor: 'pointer' }}
        onClick={() => {
          playClickSound();
          onSelectStatusFilter(currentFilter === 'out_of_stock' ? 'all' : 'out_of_stock');
        }}
        title="Click to filter out of stock items"
      >
        <div className="metric-header">
          <span className="metric-label">Depleted / Stockout</span>
          <div className="metric-icon-wrap" style={{ background: 'var(--rose-soft)', color: 'var(--rose-glow)' }}>
            <XCircle size={18} />
          </div>
        </div>
        <div className="metric-value mono-num" id="metric-out-stock-count" style={{ color: 'var(--rose-glow)' }}>
          {outOfStockCount}
        </div>
        <div className="metric-footer">
          <span style={{ color: outOfStockCount > 0 ? 'var(--rose-glow)' : 'var(--text-dim)' }}>
            {outOfStockCount > 0 ? 'Immediate procurement stoppage' : 'Zero items depleted'}
          </span>
        </div>
      </div>

      {/* 5. Health Index */}
      <div className="metric-card card-indigo" id="card-health-index">
        <div className="metric-header">
          <span className="metric-label">Supply Health Index</span>
          <div className="metric-icon-wrap" style={{ background: 'var(--indigo-soft)', color: 'var(--indigo-glow)' }}>
            <ShieldCheck size={18} />
          </div>
        </div>
        <div className="metric-value mono-num" id="metric-health-index">
          {healthPercent}%
        </div>
        <div className="metric-footer">
          <span>{healthyCount} of {totalSKUs} items in optimal range</span>
        </div>
      </div>
    </section>
  );
}
