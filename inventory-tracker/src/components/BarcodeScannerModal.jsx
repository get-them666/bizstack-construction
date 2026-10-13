import React, { useState } from 'react';
import { X, ScanLine, CheckCircle2, ArrowDownCircle, ArrowUpCircle } from 'lucide-react';
import { playScanSound, playClickSound } from '../utils/audio';

export default function BarcodeScannerModal({ isOpen, onClose, items, onAdjustStock }) {
  const [selectedSku, setSelectedSku] = useState(items[0]?.sku || '');
  const [scannedItem, setScannedItem] = useState(null);
  const [scanStatus, setScanStatus] = useState('ready'); // ready, scanning, matched

  if (!isOpen) return null;

  const handleTriggerScan = (targetSku) => {
    const skuToScan = targetSku || selectedSku;
    setScanStatus('scanning');
    
    // Simulate optical sensor sweep delay
    setTimeout(() => {
      playScanSound();
      const match = items.find((i) => i.sku.toLowerCase() === skuToScan.toLowerCase());
      setScannedItem(match || null);
      setScanStatus(match ? 'matched' : 'not_found');
    }, 450);
  };

  const handleAction = (delta, label) => {
    if (!scannedItem) return;
    playClickSound();
    onAdjustStock(scannedItem.id, delta, `Barcode Scan: ${label}`);
    // Update local preview
    setScannedItem((prev) => prev ? { ...prev, quantity: Math.max(0, prev.quantity + delta) } : null);
  };

  return (
    <div className="modal-overlay" id="modal-barcode-scanner" role="dialog" aria-modal="true">
      <div className="modal-card" style={{ maxWidth: '540px' }}>
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <ScanLine size={20} color="var(--emerald-glow)" />
            <h3 className="modal-title" id="scanner-modal-title">Optical Barcode / RFID Scanner</h3>
          </div>
          <button id="btn-close-scanner-modal" className="modal-close-btn" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <div className="modal-body">
          {/* Laser Scanner Viewport Simulation */}
          <div className="scanner-viewport" id="scanner-sensor-box">
            <div className="laser-beam" />
            <div className="scanner-guides" />
            <div style={{ position: 'absolute', bottom: '12px', left: '16px', fontSize: '0.6875rem', fontFamily: 'var(--font-mono)', color: 'var(--emerald-glow)' }}>
              SENSOR: OPTICAL LASER 650nm • RESOLUTION 4K
            </div>
          </div>

          {/* Quick Barcode Selector */}
          <div className="form-group">
            <label className="form-label" htmlFor="select-barcode-target">
              Target SKU Barcode (Select or Type)
            </label>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <select
                id="select-barcode-target"
                className="form-input"
                style={{ flex: 1 }}
                value={selectedSku}
                onChange={(e) => {
                  setSelectedSku(e.target.value);
                  handleTriggerScan(e.target.value);
                }}
              >
                {items.map((i) => (
                  <option key={i.id} value={i.sku}>
                    {i.sku} — {i.name} (Qty: {i.quantity})
                  </option>
                ))}
              </select>
              <button
                id="btn-trigger-scan"
                type="button"
                className="btn-primary"
                onClick={() => handleTriggerScan()}
              >
                Scan Now
              </button>
            </div>
          </div>

          {/* Matched Item Details */}
          {scannedItem && (
            <div style={{
              background: 'rgba(0, 0, 0, 0.4)',
              border: '1px solid var(--emerald-border)',
              borderRadius: 'var(--radius-md)',
              padding: '1rem',
              display: 'flex',
              flexDirection: 'column',
              gap: '0.75rem'
            }} id="scanned-result-card">
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span className="item-sku">{scannedItem.sku}</span>
                <span className="status-badge badge-in-stock">
                  <CheckCircle2 size={12} /> Barcode Verified
                </span>
              </div>

              <div>
                <h4 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-main)' }}>
                  {scannedItem.name}
                </h4>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)', marginTop: '0.25rem' }}>
                  Location: {scannedItem.location} ({scannedItem.bin})
                </div>
              </div>

              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', borderTop: '1px solid rgba(255,255,255,0.08)', paddingTop: '0.75rem' }}>
                <span style={{ fontSize: '0.8125rem', color: 'var(--text-muted)' }}>Current Stock Level:</span>
                <span className="mono-num" style={{ fontSize: '1.25rem', fontWeight: 800, color: 'var(--emerald-glow)' }}>
                  {scannedItem.quantity} {scannedItem.unit}
                </span>
              </div>

              {/* Instant Action Steppers */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.75rem', marginTop: '0.25rem' }}>
                <button
                  id="btn-scan-checkout"
                  className="btn-secondary"
                  style={{ justifyContent: 'center', borderColor: 'var(--rose-border)', color: 'var(--rose-glow)' }}
                  onClick={() => handleAction(-1, 'Dispense / Jobsite Draw (-1)')}
                >
                  <ArrowDownCircle size={15} /> Check-Out (-1)
                </button>
                <button
                  id="btn-scan-checkin"
                  className="btn-primary"
                  style={{ justifyContent: 'center' }}
                  onClick={() => handleAction(1, 'Intake / Restock (+1)')}
                >
                  <ArrowUpCircle size={15} /> Check-In (+1)
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="modal-footer">
          <button id="btn-close-scanner" className="btn-secondary" onClick={onClose}>
            Done Scanning
          </button>
        </div>
      </div>
    </div>
  );
}
