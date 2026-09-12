import React, { useState } from 'react';
import { Header } from './components/Header';
import { StreamList } from './components/StreamList';
import { VideoUploader } from './components/VideoUploader';
import { FolderManager } from './components/FolderManager';
import { BotSimulator } from './components/BotSimulator';
import { SettingsModal } from './components/SettingsModal';
import { VideoPlayerModal } from './components/VideoPlayerModal';

import { initialConfig, initialFolders, initialStreams } from './data/mockData';
import { AppConfig, StorageFolder, StreamItem } from './types';

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'streams' | 'uploader' | 'folders' | 'bot' | 'settings'>('streams');
  const [config, setConfig] = useState<AppConfig>(initialConfig);
  const [folders, setFolders] = useState<StorageFolder[]>(initialFolders);
  const [streams, setStreams] = useState<StreamItem[]>(initialStreams);
  const [selectedFolder, setSelectedFolder] = useState<string>('anime/solo-leveling/season-1');
  const [playingStream, setPlayingStream] = useState<StreamItem | null>(null);

  const handleSelectFolder = (folderPath: string) => {
    setSelectedFolder(folderPath);
  };

  const handleCreateFolder = (newFolderPath: string) => {
    if (folders.some((f) => f.path === newFolderPath)) return;
    const newF: StorageFolder = {
      name: newFolderPath,
      path: newFolderPath,
      fileCount: 0,
      updatedAt: new Date().toISOString().split('T')[0],
    };
    setFolders([newF, ...folders]);
  };

  const handleUploadSuccess = (newStream: StreamItem) => {
    setStreams([newStream, ...streams]);

    // Update folder file count
    if (newStream.folder_path) {
      setFolders((prev) =>
        prev.map((f) => {
          if (newStream.folder_path?.startsWith(f.path)) {
            return { ...f, fileCount: f.fileCount + 1 };
          }
          return f;
        })
      );
    }
  };

  const handleToggleLive = (id: string) => {
    setStreams((prev) =>
      prev.map((s) => (s.id === id ? { ...s, is_live: !s.is_live } : s))
    );
  };

  const handleDeleteStream = (id: string) => {
    setStreams((prev) => prev.filter((s) => s.id !== id));
  };

  const handleAddStream = (newStream: StreamItem) => {
    setStreams([newStream, ...streams]);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* Global Navigation Header */}
      <Header
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        config={config}
        selectedFolder={selectedFolder}
        streamCount={streams.length}
      />

      {/* Main Content Body */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {activeTab === 'streams' && (
          <StreamList
            streams={streams}
            onToggleLive={handleToggleLive}
            onDeleteStream={handleDeleteStream}
            onAddStream={handleAddStream}
            onPlayStream={(s) => setPlayingStream(s)}
          />
        )}

        {activeTab === 'uploader' && (
          <VideoUploader
            selectedFolder={selectedFolder}
            config={config}
            streams={streams}
            onUploadSuccess={handleUploadSuccess}
            onNavigateToFolders={() => setActiveTab('folders')}
          />
        )}

        {activeTab === 'folders' && (
          <FolderManager
            folders={folders}
            selectedFolder={selectedFolder}
            onSelectFolder={handleSelectFolder}
            onCreateFolder={handleCreateFolder}
            onNavigateToUpload={() => setActiveTab('uploader')}
            config={config}
          />
        )}

        {activeTab === 'bot' && (
          <BotSimulator
            folders={folders}
            selectedFolder={selectedFolder}
            onSelectFolder={handleSelectFolder}
            onCreateFolder={handleCreateFolder}
            config={config}
          />
        )}

        {activeTab === 'settings' && (
          <div className="max-w-3xl mx-auto">
            <SettingsModal
              config={config}
              onSaveConfig={(newConfig) => setConfig(newConfig)}
              onClose={() => setActiveTab('streams')}
            />
          </div>
        )}
      </main>

      {/* Video Player Popup Modal */}
      {playingStream && (
        <VideoPlayerModal
          stream={playingStream}
          onClose={() => setPlayingStream(null)}
        />
      )}

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950 py-6 text-center text-xs text-slate-500 font-mono">
        <div className="max-w-7xl mx-auto px-4 flex flex-col sm:flex-row items-center justify-between gap-2">
          <span>Anime4u Storage Bot — Telegram Private Channel Backend + Virtual Folders + Supabase/SQLite</span>
          <span className="text-slate-400">Production-Ready Python Bot + Control Panel</span>
        </div>
      </footer>
    </div>
  );
};

export default App;
