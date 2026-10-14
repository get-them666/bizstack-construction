import React, { useState, useEffect, useRef } from 'react';
import Header from './components/Header';
import MetricCards from './components/MetricCards';
import AnalyticsBanner from './components/AnalyticsBanner';
import ControlBar from './components/ControlBar';
import InventoryGrid from './components/InventoryGrid';
import InventoryTable from './components/InventoryTable';
import LiveActivityFeed from './components/LiveActivityFeed';
import AddItemModal from './components/AddItemModal';
import BarcodeScannerModal from './components/BarcodeScannerModal';
import ReorderPOModal from './components/ReorderPOModal';

import { INITIAL_INVENTORY } from './data/initialInventory';
import { generateRandomEvent } from './utils/realtimeSimulator';
import { 
  playStockChime, 
  playAlertSound, 
  setAudioEnabled, 
  isAudioEnabled 
} from './utils/audio';

export default function App() {
  // Inventory state with localStorage persistence
  const [items, setItems] = useState(() => {
    try {
      const saved = localStorage.getItem('nexus_inventory');
      if (saved) return JSON.parse(saved);
    } catch (e) {}
    return INITIAL_INVENTORY;
  });

  // Event stream state
  const [events, setEvents] = useState(() => {
    try {
      const saved = localStorage.getItem('nexus_events');
      if (saved) return JSON.parse(saved);
    } catch (e) {}
    // Seed initial ledger
    return [
      {
        id: 'evt-init-1',
        timestamp: new Date(Date.now() - 1000 * 60 * 2).toISOString(),
        itemId: 'item-101',
        itemName: '2x4x16 #2 Prime Kiln-Dried SPF Lumber',
        sku: 'LMB-2X4-16',
        type: 'inbound',
        delta: 40,
        unit: 'pcs',
        operator: 'Elena Rostova',
        role: 'Warehouse Mgr',
        reason: 'Supplier direct drop off'
      },
      {
        id: 'evt-init-2',
        timestamp: new Date(Date.now() - 1000 * 60 * 7).toISOString(),
        itemId: 'item-105',
        itemName: 'Southwire Romex SIMpull 12/2 NM-B Cable (250 ft.)',
        sku: 'ELC-ROMEX-12',
        type: 'outbound',
        delta: -4,
        unit: 'spools',
        operator: 'Dave K.',
        role: 'Lead Electrician',
        reason: 'Immediate crew dispatch consumption'
      }
    ];
  });

  // UI & Filter state
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedCategory, setSelectedCategory] = useState('All');
  const [statusFilter, setStatusFilter] = useState('all');
  const [locationFilter, setLocationFilter] = useState('all');
  const [sortBy, setSortBy] = useState('stock_asc');
  const [viewMode, setViewMode] = useState('grid');
  const [sidebarOpen, setSidebarOpen] = useState(true);

  // Simulation controls
  const [isSimulating, setIsSimulating] = useState(true);
  const [simSpeed, setSimSpeed] = useState(1); // 1x, 3x, 5x
  const [soundEnabled, setSoundState] = useState(isAudioEnabled);
  const [recentlyUpdatedId, setRecentlyUpdatedId] = useState(null);

  // Modals state
  const [isAddModalOpen, setIsAddModalOpen] = useState(false);
  const [editingItem, setEditingItem] = useState(null);
  const [isScanModalOpen, setIsScanModalOpen] = useState(false);
  const [isPOModalOpen, setIsPOModalOpen] = useState(false);

  // Sync inventory to localStorage
  useEffect(() => {
    try {
      localStorage.setItem('nexus_inventory', JSON.stringify(items));
    } catch (e) {}
  }, [items]);

  // Sync events to localStorage
  useEffect(() => {
    try {
      localStorage.setItem('nexus_events', JSON.stringify(events.slice(0, 50)));
    } catch (e) {}
  }, [events]);

  // Real-time Event Simulation Loop
  const itemsRef = useRef(items);
  itemsRef.current = items;

  useEffect(() => {
    if (!isSimulating) return;

    const intervalMs = simSpeed === 1 ? 5500 : simSpeed === 3 ? 2400 : 1000;

    const timer = setInterval(() => {
      const simResult = generateRandomEvent(itemsRef.current);
      if (!simResult) return;

      const { targetItemId, newQuantity, event } = simResult;

      // Update state
      setItems((prevItems) =>
        prevItems.map((item) => {
          if (item.id === targetItemId) {
            const isNowLow = newQuantity <= item.reorderLevel;
            if (isNowLow && item.quantity > item.reorderLevel) {
              playAlertSound();
            } else if (event.delta > 0) {
              playStockChime();
            }
            return {
              ...item,
              quantity: newQuantity,
              lastUpdated: new Date().toISOString()
            };
          }
          return item;
        })
      );

      // Add to event stream
      setEvents((prevEvents) => [event, ...prevEvents.slice(0, 49)]);

      // Flash highlight
      setRecentlyUpdatedId(targetItemId);
      setTimeout(() => {
        setRecentlyUpdatedId((curr) => (curr === targetItemId ? null : curr));
      }, 1200);
    }, intervalMs);

    return () => clearInterval(timer);
  }, [isSimulating, simSpeed]);

  // Force Manual Simulation Event
  const handleForceEvent = () => {
    const simResult = generateRandomEvent(items);
    if (!simResult) return;

    const { targetItemId, newQuantity, event } = simResult;
    setItems((prevItems) =>
      prevItems.map((item) =>
        item.id === targetItemId
          ? { ...item, quantity: newQuantity, lastUpdated: new Date().toISOString() }
          : item
      )
    );
    setEvents((prev) => [event, ...prev.slice(0, 49)]);
    setRecentlyUpdatedId(targetItemId);
    setTimeout(() => {
      setRecentlyUpdatedId(null);
    }, 1200);
  };

  // Sound toggle
  const handleToggleSound = () => {
    const nextVal = !soundEnabled;
    setSoundState(nextVal);
    setAudioEnabled(nextVal);
  };

  // Stock Adjustment Handler
  const handleAdjustStock = (itemId, delta, reason = 'Direct Adjustment') => {
    const item = items.find((i) => i.id === itemId);
    if (!item) return;

    const newQty = Math.max(0, item.quantity + delta);
    const actualDelta = newQty - item.quantity;
    if (actualDelta === 0) return;

    const newEvent = {
      id: `evt-${Date.now()}`,
      timestamp: new Date().toISOString(),
      itemId: item.id,
      itemName: item.name,
      sku: item.sku,
      type: actualDelta > 0 ? 'inbound' : 'outbound',
      delta: actualDelta,
      unit: item.unit,
      operator: 'Command Operator',
      role: 'Portal Admin',
      reason
    };

    setItems((prev) =>
      prev.map((i) =>
        i.id === itemId
          ? { ...i, quantity: newQty, lastUpdated: new Date().toISOString() }
          : i
      )
    );
    setEvents((prev) => [newEvent, ...prev.slice(0, 49)]);
    setRecentlyUpdatedId(itemId);
    setTimeout(() => setRecentlyUpdatedId(null), 1200);
  };

  // Quick Restock to Max Capacity
  const handleQuickRestock = (itemId) => {
    const item = items.find((i) => i.id === itemId);
    if (!item) return;

    const max = item.maxCapacity || 100;
    const delta = Math.max(0, max - item.quantity);
    if (delta > 0) {
      handleAdjustStock(itemId, delta, 'Quick replenishment to capacity');
    }
  };

  // Save / Update Item
  const handleSaveItem = (itemData) => {
    if (editingItem) {
      setItems((prev) => prev.map((i) => (i.id === itemData.id ? itemData : i)));
    } else {
      setItems((prev) => [itemData, ...prev]);
    }
    setEditingItem(null);
  };

  // Delete Item
  const handleDeleteItem = (itemId) => {
    setItems((prev) => prev.filter((i) => i.id !== itemId));
  };

  // Execute Reorder Purchase Order
  const handleExecutePO = (orders, poNumber) => {
    setItems((prev) =>
      prev.map((item) => {
        const order = orders.find((o) => o.itemId === item.id);
        if (order) {
          return {
            ...item,
            quantity: item.quantity + order.addedQty,
            lastUpdated: new Date().toISOString()
          };
        }
        return item;
      })
    );

    // Add aggregate ledger event
    const poEvent = {
      id: `evt-${Date.now()}`,
      timestamp: new Date().toISOString(),
      itemId: 'po-bulk',
      itemName: `Bulk PO Delivery (${orders.length} SKUs)`,
      sku: poNumber,
      type: 'inbound',
      delta: orders.reduce((sum, o) => sum + o.addedQty, 0),
      unit: 'items',
      operator: 'Automated Procurement',
      role: 'System Bot',
      reason: `Replenishment fulfilled under ${poNumber}`
    };
    setEvents((prev) => [poEvent, ...prev.slice(0, 49)]);
  };

  // Export full inventory CSV
  const handleExportCSV = () => {
    const headers = [
      'SKU',
      'Item_Name',
      'Category',
      'Quantity_On_Hand',
      'Unit',
      'Reorder_Level',
      'Max_Capacity',
      'Unit_Cost',
      'Total_Valuation',
      'Location',
      'Bin',
      'Supplier',
      'Last_Updated'
    ];

    const rows = filteredItems.map((i) => [
      i.sku,
      `"${i.name}"`,
      `"${i.category}"`,
      i.quantity,
      i.unit,
      i.reorderLevel,
      i.maxCapacity,
      i.unitCost.toFixed(2),
      (i.quantity * i.unitCost).toFixed(2),
      `"${i.location}"`,
      `"${i.bin}"`,
      `"${i.supplier}"`,
      i.lastUpdated
    ]);

    const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement('a');
    link.setAttribute('href', encodedUri);
    link.setAttribute('download', `NEXUS_Inventory_Snapshot_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  // Filtering & Sorting pipeline
  const filteredItems = items.filter((item) => {
    // Search query
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      const matchName = item.name.toLowerCase().includes(q);
      const matchSku = item.sku.toLowerCase().includes(q);
      const matchSupplier = (item.supplier || '').toLowerCase().includes(q);
      const matchBin = (item.bin || '').toLowerCase().includes(q);
      const matchCat = (item.category || '').toLowerCase().includes(q);
      if (!matchName && !matchSku && !matchSupplier && !matchBin && !matchCat) {
        return false;
      }
    }

    // Category
    if (selectedCategory !== 'All' && item.category !== selectedCategory) {
      return false;
    }

    // Status filter
    if (statusFilter === 'optimal' && item.quantity <= item.reorderLevel) return false;
    if (statusFilter === 'low' && (item.quantity === 0 || item.quantity > item.reorderLevel)) return false;
    if (statusFilter === 'out_of_stock' && item.quantity > 0) return false;

    // Location filter
    if (locationFilter !== 'all' && item.location !== locationFilter) {
      return false;
    }

    return true;
  }).sort((a, b) => {
    if (sortBy === 'stock_asc') return a.quantity - b.quantity;
    if (sortBy === 'stock_desc') return b.quantity - a.quantity;
    if (sortBy === 'val_desc') return (b.quantity * b.unitCost) - (a.quantity * a.unitCost);
    if (sortBy === 'name_asc') return a.name.localeCompare(b.name);
    if (sortBy === 'recent') return new Date(b.lastUpdated || 0) - new Date(a.lastUpdated || 0);
    return 0;
  });

  const lowStockCount = items.filter((i) => i.quantity <= i.reorderLevel).length;

  return (
    <div className="app-container" id="nexus-app-root">
      {/* 1. Header with Live Telemetry Controls */}
      <Header
        isSimulating={isSimulating}
        onToggleSimulate={() => setIsSimulating(!isSimulating)}
        simSpeed={simSpeed}
        onChangeSimSpeed={setSimSpeed}
        onForceEvent={handleForceEvent}
        soundEnabled={soundEnabled}
        onToggleSound={handleToggleSound}
        unreadEventsCount={events.length}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen(!sidebarOpen)}
      />

      {/* 2. Executive Metric KPIs */}
      <MetricCards
        items={items}
        currentFilter={statusFilter}
        onSelectStatusFilter={setStatusFilter}
      />

      {/* 3. Category & Capital Analytics Strip */}
      <AnalyticsBanner
        items={items}
        selectedCategory={selectedCategory}
        onSelectCategory={setSelectedCategory}
      />

      {/* 4. Filter Toolbar & Search */}
      <ControlBar
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        selectedCategory={selectedCategory}
        onSelectCategory={setSelectedCategory}
        statusFilter={statusFilter}
        onStatusFilterChange={setStatusFilter}
        locationFilter={locationFilter}
        onLocationFilterChange={setLocationFilter}
        sortBy={sortBy}
        onSortChange={setSortBy}
        viewMode={viewMode}
        onToggleViewMode={setViewMode}
        onOpenAddModal={() => {
          setEditingItem(null);
          setIsAddModalOpen(true);
        }}
        onOpenScanModal={() => setIsScanModalOpen(true)}
        onOpenPOModal={() => setIsPOModalOpen(true)}
        onExportCSV={handleExportCSV}
        lowStockItemsCount={lowStockCount}
      />

      {/* 5. Workspace: Inventory Display + Live Activity Ledger */}
      <main className={`workspace-layout ${!sidebarOpen ? 'sidebar-collapsed' : ''}`} id="main-inventory-workspace">
        <section className="inventory-view-area" id="inventory-catalog-section" aria-label="Inventory Catalog">
          {viewMode === 'grid' ? (
            <InventoryGrid
              items={filteredItems}
              recentlyUpdatedId={recentlyUpdatedId}
              onAdjustStock={handleAdjustStock}
              onOpenEditModal={(item) => {
                setEditingItem(item);
                setIsAddModalOpen(true);
              }}
              onDeleteItem={handleDeleteItem}
              onQuickRestock={handleQuickRestock}
            />
          ) : (
            <InventoryTable
              items={filteredItems}
              recentlyUpdatedId={recentlyUpdatedId}
              onAdjustStock={handleAdjustStock}
              onOpenEditModal={(item) => {
                setEditingItem(item);
                setIsAddModalOpen(true);
              }}
              onDeleteItem={handleDeleteItem}
              onQuickRestock={handleQuickRestock}
            />
          )}
        </section>

        {/* Live Activity Stream Sidebar */}
        {sidebarOpen && (
          <LiveActivityFeed
            events={events}
            onClearEvents={() => setEvents([])}
            onCloseSidebar={() => setSidebarOpen(false)}
          />
        )}
      </main>

      {/* Modals */}
      <AddItemModal
        isOpen={isAddModalOpen}
        onClose={() => setIsAddModalOpen(false)}
        onSaveItem={handleSaveItem}
        editItem={editingItem}
      />

      <BarcodeScannerModal
        isOpen={isScanModalOpen}
        onClose={() => setIsScanModalOpen(false)}
        items={items}
        onAdjustStock={handleAdjustStock}
      />

      <ReorderPOModal
        isOpen={isPOModalOpen}
        onClose={() => setIsPOModalOpen(false)}
        items={items}
        onExecutePO={handleExecutePO}
      />
    </div>
  );
}
