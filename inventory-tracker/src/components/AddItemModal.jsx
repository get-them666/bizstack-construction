import React, { useState, useEffect } from 'react';
import { X, Sparkles } from 'lucide-react';
import { CATEGORIES, LOCATIONS } from '../data/initialInventory';
import { playClickSound, playStockChime } from '../utils/audio';

export default function AddItemModal({ isOpen, onClose, onSaveItem, editItem }) {
  const [formData, setFormData] = useState({
    name: '',
    sku: '',
    category: 'Framing & Lumber',
    quantity: 50,
    reorderLevel: 20,
    maxCapacity: 100,
    unit: 'pcs',
    unitCost: 15.00,
    location: 'Norfolk Central Hub',
    bin: 'Shelf A1',
    supplier: 'Builders FirstSource'
  });

  useEffect(() => {
    if (editItem) {
      setFormData({ ...editItem });
    } else {
      setFormData({
        name: '',
        sku: '',
        category: 'Framing & Lumber',
        quantity: 50,
        reorderLevel: 20,
        maxCapacity: 100,
        unit: 'pcs',
        unitCost: 15.00,
        location: 'Norfolk Central Hub',
        bin: 'Shelf A1',
        supplier: 'Builders FirstSource'
      });
    }
  }, [editItem, isOpen]);

  if (!isOpen) return null;

  const handleGenerateSKU = () => {
    playClickSound();
    const prefix = formData.category.substring(0, 3).toUpperCase();
    const randomCode = Math.floor(100 + Math.random() * 900);
    const sku = `${prefix}-${randomCode}-GEN`;
    setFormData((prev) => ({ ...prev, sku }));
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!formData.name.trim() || !formData.sku.trim()) return;

    playStockChime();
    onSaveItem({
      ...formData,
      quantity: Number(formData.quantity) || 0,
      reorderLevel: Number(formData.reorderLevel) || 0,
      maxCapacity: Number(formData.maxCapacity) || 100,
      unitCost: Number(formData.unitCost) || 0,
      id: editItem ? editItem.id : `item-${Date.now()}`,
      lastUpdated: new Date().toISOString()
    });
    onClose();
  };

  return (
    <div className="modal-overlay" id="modal-add-item" role="dialog" aria-modal="true">
      <div className="modal-card">
        <div className="modal-header">
          <h3 className="modal-title" id="modal-item-title">
            {editItem ? 'Edit SKU Specification' : 'Register New Inventory SKU'}
          </h3>
          <button 
            id="btn-close-item-modal"
            className="modal-close-btn" 
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            {/* Item Name */}
            <div className="form-group">
              <label className="form-label" htmlFor="input-item-name">Item Description / Name *</label>
              <input
                id="input-item-name"
                className="form-input"
                required
                placeholder="e.g. 2x4x16 #2 Prime Kiln-Dried Lumber"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              />
            </div>

            {/* SKU with Auto-Generator */}
            <div className="form-grid-2">
              <div className="form-group">
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <label className="form-label" htmlFor="input-item-sku">SKU Code *</label>
                  <button
                    type="button"
                    onClick={handleGenerateSKU}
                    style={{ background: 'none', border: 'none', color: 'var(--cyan-glow)', fontSize: '0.75rem', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.25rem' }}
                  >
                    <Sparkles size={11} /> Auto-Generate
                  </button>
                </div>
                <input
                  id="input-item-sku"
                  className="form-input"
                  required
                  placeholder="e.g. LMB-2X4-16"
                  value={formData.sku}
                  onChange={(e) => setFormData({ ...formData, sku: e.target.value })}
                />
              </div>

              {/* Category */}
              <div className="form-group">
                <label className="form-label" htmlFor="select-item-category">Category</label>
                <select
                  id="select-item-category"
                  className="form-input"
                  value={formData.category}
                  onChange={(e) => setFormData({ ...formData, category: e.target.value })}
                >
                  {CATEGORIES.filter((c) => c !== 'All').map((cat) => (
                    <option key={cat} value={cat}>{cat}</option>
                  ))}
                </select>
              </div>
            </div>

            {/* Quantities & Capacity */}
            <div className="form-grid-2">
              <div className="form-group">
                <label className="form-label" htmlFor="input-item-qty">Initial Quantity On-Hand</label>
                <input
                  id="input-item-qty"
                  type="number"
                  className="form-input"
                  min="0"
                  value={formData.quantity}
                  onChange={(e) => setFormData({ ...formData, quantity: e.target.value })}
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="input-item-unit">Unit of Measure</label>
                <input
                  id="input-item-unit"
                  className="form-input"
                  placeholder="pcs, bundles, sheets, spools..."
                  value={formData.unit}
                  onChange={(e) => setFormData({ ...formData, unit: e.target.value })}
                />
              </div>
            </div>

            {/* Thresholds & Costs */}
            <div className="form-grid-2">
              <div className="form-group">
                <label className="form-label" htmlFor="input-item-reorder">Reorder Alert Threshold</label>
                <input
                  id="input-item-reorder"
                  type="number"
                  className="form-input"
                  min="0"
                  value={formData.reorderLevel}
                  onChange={(e) => setFormData({ ...formData, reorderLevel: e.target.value })}
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="input-item-capacity">Max Bin Capacity</label>
                <input
                  id="input-item-capacity"
                  type="number"
                  className="form-input"
                  min="1"
                  value={formData.maxCapacity}
                  onChange={(e) => setFormData({ ...formData, maxCapacity: e.target.value })}
                />
              </div>
            </div>

            <div className="form-grid-2">
              <div className="form-group">
                <label className="form-label" htmlFor="input-item-cost">Unit Cost ($ USD)</label>
                <input
                  id="input-item-cost"
                  type="number"
                  step="0.01"
                  className="form-input"
                  value={formData.unitCost}
                  onChange={(e) => setFormData({ ...formData, unitCost: e.target.value })}
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="select-item-location">Warehouse Location</label>
                <select
                  id="select-item-location"
                  className="form-input"
                  value={formData.location}
                  onChange={(e) => setFormData({ ...formData, location: e.target.value })}
                >
                  {LOCATIONS.filter((l) => l !== 'All Locations').map((loc) => (
                    <option key={loc} value={loc}>{loc}</option>
                  ))}
                </select>
              </div>
            </div>

            <div className="form-grid-2">
              <div className="form-group">
                <label className="form-label" htmlFor="input-item-bin">Bin / Shelf Coordinate</label>
                <input
                  id="input-item-bin"
                  className="form-input"
                  placeholder="e.g. Bay 4 - Rack A"
                  value={formData.bin}
                  onChange={(e) => setFormData({ ...formData, bin: e.target.value })}
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="input-item-supplier">Preferred Supplier</label>
                <input
                  id="input-item-supplier"
                  className="form-input"
                  placeholder="e.g. ABC Supply Co."
                  value={formData.supplier}
                  onChange={(e) => setFormData({ ...formData, supplier: e.target.value })}
                />
              </div>
            </div>
          </div>

          <div className="modal-footer">
            <button
              id="btn-cancel-modal"
              type="button"
              className="btn-secondary"
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              id="btn-save-item"
              type="submit"
              className="btn-primary"
            >
              {editItem ? 'Update Specification' : 'Save & Register SKU'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
