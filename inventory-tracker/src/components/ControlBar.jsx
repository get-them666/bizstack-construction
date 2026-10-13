import React from 'react';
import { 
  Search, 
  Plus, 
  ScanLine, 
  FileText, 
  Download, 
  LayoutGrid, 
  List, 
  Filter,
  MapPin,
  ArrowUpDown
} from 'lucide-react';
import { CATEGORIES, LOCATIONS } from '../data/initialInventory';
import { playClickSound } from '../utils/audio';

export default function ControlBar({
  searchQuery,
  onSearchChange,
  selectedCategory,
  onSelectCategory,
  statusFilter,
  onStatusFilterChange,
  locationFilter,
  onLocationFilterChange,
  sortBy,
  onSortChange,
  viewMode,
  onToggleViewMode,
  onOpenAddModal,
  onOpenScanModal,
  onOpenPOModal,
  onExportCSV,
  lowStockItemsCount
}) {
  return (
    <section className="toolbar-section" id="inventory-toolbar" aria-label="Inventory Controls & Filters">
      {/* Primary Toolbar Row */}
      <div className="toolbar-primary">
        {/* Search Input */}
        <div className="search-input-wrap">
          <Search size={16} className="search-icon" />
          <input
            id="input-inventory-search"
            type="text"
            className="search-input"
            placeholder="Search by SKU, item name, supplier, bin..."
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
          />
        </div>

        {/* Action Buttons */}
        <div className="action-buttons-group">
          {/* Add New SKU */}
          <button
            id="btn-add-sku"
            className="btn-primary"
            onClick={() => {
              playClickSound();
              onOpenAddModal();
            }}
          >
            <Plus size={16} strokeWidth={2.5} />
            <span>Add SKU</span>
          </button>

          {/* Barcode Scanner */}
          <button
            id="btn-open-scanner"
            className="btn-secondary"
            onClick={() => {
              playClickSound();
              onOpenScanModal();
            }}
            title="Launch interactive camera & barcode scanner"
          >
            <ScanLine size={16} />
            <span>Scan Barcode</span>
          </button>

          {/* Reorder PO Generator */}
          <button
            id="btn-open-po-generator"
            className="btn-accent"
            onClick={() => {
              playClickSound();
              onOpenPOModal();
            }}
            title="Generate automated procurement purchase order"
          >
            <FileText size={16} />
            <span>Reorder PO</span>
            {lowStockItemsCount > 0 && (
              <span style={{ 
                background: '#f43f5e', 
                color: '#fff', 
                borderRadius: '9999px', 
                fontSize: '0.6875rem', 
                padding: '0 0.4rem', 
                fontWeight: 800 
              }}>
                {lowStockItemsCount}
              </span>
            )}
          </button>

          {/* Export CSV */}
          <button
            id="btn-export-csv"
            className="btn-secondary"
            onClick={() => {
              playClickSound();
              onExportCSV();
            }}
            title="Download CSV manifest of current inventory"
          >
            <Download size={16} />
            <span>Export CSV</span>
          </button>
        </div>
      </div>

      {/* Secondary Toolbar Row: Categories & Filters */}
      <div className="toolbar-filters">
        {/* Category Pills */}
        <div className="pill-group" id="category-pills" role="tablist">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              id={`pill-category-${cat.toLowerCase().replace(/[^a-z0-9]/g, '-')}`}
              className={`category-pill ${selectedCategory === cat ? 'active' : ''}`}
              onClick={() => {
                playClickSound();
                onSelectCategory(cat);
              }}
              role="tab"
              aria-selected={selectedCategory === cat}
            >
              {cat}
            </button>
          ))}
        </div>

        {/* Dropdown Filters & View Switcher */}
        <div className="filter-controls-group">
          {/* Status Filter */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
            <Filter size={14} color="var(--text-dim)" />
            <select
              id="select-status-filter"
              className="select-custom"
              value={statusFilter}
              onChange={(e) => onStatusFilterChange(e.target.value)}
            >
              <option value="all">Status: All Levels</option>
              <option value="optimal">In Stock (Optimal)</option>
              <option value="low">Low Stock (Alert)</option>
              <option value="out_of_stock">Out of Stock</option>
            </select>
          </div>

          {/* Location Filter */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
            <MapPin size={14} color="var(--text-dim)" />
            <select
              id="select-location-filter"
              className="select-custom"
              value={locationFilter}
              onChange={(e) => onLocationFilterChange(e.target.value)}
            >
              {LOCATIONS.map((loc) => (
                <option key={loc} value={loc === 'All Locations' ? 'all' : loc}>
                  {loc}
                </option>
              ))}
            </select>
          </div>

          {/* Sort By */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
            <ArrowUpDown size={14} color="var(--text-dim)" />
            <select
              id="select-sort-by"
              className="select-custom"
              value={sortBy}
              onChange={(e) => onSortChange(e.target.value)}
            >
              <option value="stock_asc">Sort: Stock (Low to High)</option>
              <option value="stock_desc">Sort: Stock (High to Low)</option>
              <option value="val_desc">Sort: Value ($ High to Low)</option>
              <option value="name_asc">Sort: Name (A-Z)</option>
              <option value="recent">Sort: Recently Updated</option>
            </select>
          </div>

          {/* Grid vs Table View Mode */}
          <div className="view-toggle" id="view-mode-toggle">
            <button
              id="btn-view-grid"
              className={`view-toggle-btn ${viewMode === 'grid' ? 'active' : ''}`}
              onClick={() => onToggleViewMode('grid')}
              title="Grid View"
            >
              <LayoutGrid size={15} />
            </button>
            <button
              id="btn-view-table"
              className={`view-toggle-btn ${viewMode === 'table' ? 'active' : ''}`}
              onClick={() => onToggleViewMode('table')}
              title="Table View"
            >
              <List size={15} />
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
