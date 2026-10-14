import React, { useState, useEffect } from 'react';
import { 
  Boxes, 
  Radio, 
  Play, 
  Pause, 
  Zap, 
  Volume2, 
  VolumeX, 
  Activity,
  Clock
} from 'lucide-react';
import { playClickSound } from '../utils/audio';

export default function Header({
  isSimulating,
  onToggleSimulate,
  simSpeed,
  onChangeSimSpeed,
  onForceEvent,
  soundEnabled,
  onToggleSound,
  unreadEventsCount,
  onToggleSidebar,
  sidebarOpen
}) {
  const [time, setTime] = useState('');

  useEffect(() => {
    const updateTime = () => {
      const now = new Date();
      setTime(now.toLocaleTimeString('en-US', { hour12: false }));
    };
    updateTime();
    const timer = setInterval(updateTime, 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <header className="main-header" id="app-header">
      <div className="brand-section">
        <div className="brand-logo-icon" id="brand-logo">
          <Boxes size={26} strokeWidth={2.2} />
        </div>
        <div className="brand-titles">
          <span className="brand-badge">TELEMETRY v2.4 // ENTERPRISE</span>
          <h1 className="main-title" id="page-heading">NEXUS Stock Intelligence</h1>
        </div>
      </div>

      <div className="telemetry-controls" id="telemetry-bar">
        {/* Live Pulse Indicator */}
        <div className="live-beacon" id="live-sync-indicator" title="Live WebSocket Simulation Status">
          <span className={`beacon-dot ${!isSimulating ? 'paused' : ''}`}></span>
          <span>{isSimulating ? 'LIVE STREAM' : 'PAUSED'}</span>
        </div>

        {/* Play/Pause Button */}
        <button
          id="btn-toggle-simulation"
          className={`ctrl-btn ${isSimulating ? 'active' : ''}`}
          onClick={() => {
            playClickSound();
            onToggleSimulate();
          }}
          title={isSimulating ? "Pause real-time stream" : "Start real-time stream"}
        >
          {isSimulating ? <Pause size={14} /> : <Play size={14} />}
          <span>{isSimulating ? 'Pause' : 'Stream'}</span>
        </button>

        {/* Speed Controls */}
        <button
          id="btn-sim-speed"
          className="ctrl-btn"
          onClick={() => {
            playClickSound();
            const nextSpeed = simSpeed === 1 ? 3 : simSpeed === 3 ? 5 : 1;
            onChangeSimSpeed(nextSpeed);
          }}
          title="Adjust simulation frequency"
        >
          <Zap size={14} />
          <span>{simSpeed}x Speed</span>
        </button>

        {/* Force Manual Event */}
        <button
          id="btn-force-event"
          className="ctrl-btn btn-pulse"
          onClick={() => {
            playClickSound();
            onForceEvent();
          }}
          title="Trigger instantaneous inventory event"
        >
          <Radio size={14} />
          <span>Simulate Event</span>
        </button>

        {/* Sound Toggle */}
        <button
          id="btn-toggle-sound"
          className={`ctrl-btn ${soundEnabled ? 'active' : ''}`}
          onClick={() => {
            playClickSound();
            onToggleSound();
          }}
          title={soundEnabled ? "Mute audio synthesizer" : "Enable real-time audio alerts"}
        >
          {soundEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
          <span>{soundEnabled ? 'Audio On' : 'Muted'}</span>
        </button>

        {/* Activity Stream Drawer Button */}
        <button
          id="btn-toggle-activity-feed"
          className={`ctrl-btn ${sidebarOpen ? 'active' : ''}`}
          onClick={() => {
            playClickSound();
            onToggleSidebar();
          }}
          title="Toggle live activity ledger"
        >
          <Activity size={14} />
          <span>Activity</span>
          {unreadEventsCount > 0 && (
            <span className="ledger-count" style={{ background: 'rgba(16,185,129,0.3)', color: '#10b981' }}>
              {unreadEventsCount}
            </span>
          )}
        </button>

        {/* Clock & Ping */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: 'var(--text-dim)', fontSize: '0.75rem', fontFamily: 'var(--font-mono)', paddingLeft: '0.25rem' }}>
          <Clock size={12} />
          <span>{time}</span>
          <span style={{ color: '#10b981' }}>• 4ms</span>
        </div>
      </div>
    </header>
  );
}
