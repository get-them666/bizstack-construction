import React from 'react';
import { PieChart } from 'lucide-react';

const CATEGORY_COLORS = {
  "Framing & Lumber": "#10b981",
  "Roofing & Siding": "#06b6d4",
  "Electrical & Lighting": "#f59e0b",
  "Plumbing & Mechanical": "#3b82f6",
  "Fasteners & Hardware": "#8b5cf6",
  "Safety & Tools": "#ec4899",
  "Other": "#94a3b8"
};

export default function AnalyticsBanner({ items, onSelectCategory, selectedCategory }) {
  // Aggregate items count and value per category
  const categoryStats = items.reduce((acc, item) => {
    const cat = item.category || 'Other';
    if (!acc[cat]) {
      acc[cat] = { count: 0, totalValue: 0 };
    }
    acc[cat].count += 1;
    acc[cat].totalValue += (item.quantity * item.unitCost);
    return acc;
  }, {});

  const totalItemCount = items.length || 1;

  return (
    <section className="analytics-bar" id="analytics-distribution-section" aria-label="Inventory Distribution">
      <div className="analytics-title-row">
        <div className="analytics-heading">
          <PieChart size={16} color="var(--cyan-glow)" />
          <span>Category Inventory Share &amp; Capital Allocation</span>
        </div>
        <span style={{ fontSize: '0.75rem', color: 'var(--text-dim)', fontFamily: 'var(--font-mono)' }}>
          {items.length} Tracked Catalog Entries
        </span>
      </div>

      {/* Visual multi-segment distribution track */}
      <div className="distribution-track" id="category-distribution-track">
        {Object.entries(categoryStats).map(([cat, stats]) => {
          const percent = (stats.count / totalItemCount) * 100;
          const color = CATEGORY_COLORS[cat] || CATEGORY_COLORS['Other'];
          return (
            <div
              key={cat}
              className="distribution-segment"
              style={{
                width: `${percent}%`,
                backgroundColor: color
              }}
              title={`${cat}: ${stats.count} items (${Math.round(percent)}%) - $${stats.totalValue.toLocaleString()}`}
            />
          );
        })}
      </div>

      {/* Distribution Legend */}
      <div className="distribution-legend" id="category-distribution-legend">
        {Object.entries(categoryStats).map(([cat, stats]) => {
          const color = CATEGORY_COLORS[cat] || CATEGORY_COLORS['Other'];
          const isSelected = selectedCategory === cat;
          return (
            <div
              key={cat}
              className="legend-item"
              style={{
                cursor: 'pointer',
                opacity: selectedCategory === 'All' || isSelected ? 1 : 0.5,
                fontWeight: isSelected ? 700 : 500
              }}
              onClick={() => onSelectCategory(isSelected ? 'All' : cat)}
            >
              <span className="legend-swatch" style={{ backgroundColor: color }} />
              <span>{cat}</span>
              <span className="mono-num" style={{ color: 'var(--text-dim)' }}>
                ({stats.count})
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
