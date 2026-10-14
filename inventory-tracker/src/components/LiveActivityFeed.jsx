import React from 'react';
import { 
  Activity, 
  ArrowDownRight, 
  ArrowUpRight, 
  RotateCcw, 
  Trash2, 
  X 
} from 'lucide-react';
import { playClickSound } from '../utils/audio';

export default function LiveActivityFeed({
  events,
  onClearEvents,
  onCloseSidebar
}) {
  const formatTimeDelta = (isoString) => {
    const diffSeconds = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
    if (diffSeconds < 5) return 'Just now';
    if (diffSeconds < 60) return `${diffSeconds}s ago`;
    const diffMinutes = Math.floor(diffSeconds / 60);
    if (diffMinutes < 60) return `${diffMinutes}m ago`;
    return new Date(isoString).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  return (
    <aside className="live-ledger-sidebar" id="live-activity-ledger" aria-label="Live Event Stream">
      <div className="ledger-header">
        <div className="ledger-title">
          <Activity size={16} color="var(--emerald-glow)" />
          <span>Real-Time Stream</span>
          <span className="ledger-count">{events.length}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.375rem' }}>
          <button
            id="btn-clear-ledger"
            className="adj-btn"
            onClick={() => {
              playClickSound();
              onClearEvents();
            }}
            title="Clear Event Log"
          >
            <Trash2 size={12} />
          </button>
          <button
            id="btn-close-ledger-sidebar"
            className="adj-btn"
            onClick={() => {
              playClickSound();
              onCloseSidebar();
            }}
            title="Close Sidebar"
          >
            <X size={13} />
          </button>
        </div>
      </div>

      <div className="ledger-scroll" id="ledger-entries-list">
        {events.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '2rem 1rem', color: 'var(--text-dim)', fontSize: '0.8125rem' }}>
            <Activity size={24} style={{ opacity: 0.3, margin: '0 auto 0.5rem auto' }} />
            No events logged yet. Real-time transactions will appear as they occur.
          </div>
        ) : (
          events.map((evt) => {
            const isPositive = evt.delta > 0;
            const isZero = evt.delta === 0;
            const deltaClass = isPositive ? 'positive' : isZero ? 'neutral' : 'negative';
            const entryTypeClass = evt.type === 'inbound' 
              ? 'entry-inbound' 
              : evt.type === 'audit' 
              ? 'entry-audit' 
              : 'entry-outbound';

            return (
              <div 
                key={evt.id} 
                className={`ledger-entry ${entryTypeClass}`}
                id={`ledger-entry-${evt.id}`}
              >
                <div className="ledger-entry-top">
                  <span className="ledger-op-name" title={evt.itemName}>
                    {evt.itemName}
                  </span>
                  <span className={`ledger-delta ${deltaClass}`}>
                    {isPositive ? `+${evt.delta}` : evt.delta} {evt.unit}
                  </span>
                </div>

                <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
                  {evt.reason || 'Inventory movement'}
                </div>

                <div className="ledger-entry-meta">
                  <span style={{ color: 'var(--cyan-glow)' }}>
                    {evt.operator} ({evt.role})
                  </span>
                  <span>{formatTimeDelta(evt.timestamp)}</span>
                </div>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}
