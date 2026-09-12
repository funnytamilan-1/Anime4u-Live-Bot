import React, { useState } from 'react';
import { 
  Folder, 
  FolderPlus, 
  CheckCircle2, 
  Search, 
  RefreshCw, 
  HardDrive, 
  ArrowRight,
  UploadCloud,
  FileVideo,
  Send
} from 'lucide-react';
import { StorageFolder, AppConfig } from '../types';

interface FolderManagerProps {
  folders: StorageFolder[];
  selectedFolder: string;
  onSelectFolder: (folderPath: string) => void;
  onCreateFolder: (newFolderPath: string) => void;
  onNavigateToUpload: () => void;
  config: AppConfig;
}

export const FolderManager: React.FC<FolderManagerProps> = ({
  folders,
  selectedFolder,
  onSelectFolder,
  onCreateFolder,
  onNavigateToUpload,
  config,
}) => {
  const [searchTerm, setSearchTerm] = useState('');
  const [newFolderName, setNewFolderName] = useState('');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const filteredFolders = folders.filter((f) =>
    f.name.toLowerCase().includes(searchTerm.toLowerCase())
  );

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    const cleaned = newFolderName.trim().replace(/^[\/\s]+|[\/\s]+$/g, '');
    if (!cleaned) return;
    if (cleaned.includes('..') || cleaned.includes('\\')) {
      alert('Invalid folder path');
      return;
    }
    onCreateFolder(cleaned);
    onSelectFolder(cleaned);
    setNewFolderName('');
    setShowCreateModal(false);
  };

  const handleRefresh = () => {
    setIsRefreshing(true);
    setTimeout(() => {
      setIsRefreshing(false);
    }, 600);
  };

  return (
    <div className="space-y-6">
      {/* Banner / Info Header */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 relative overflow-hidden">
        <div className="absolute top-0 right-0 w-96 h-96 bg-indigo-500/10 rounded-full blur-3xl -z-0 pointer-events-none" />
        
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 relative z-10">
          <div>
            <div className="flex items-center space-x-2 text-indigo-400 text-xs font-semibold uppercase tracking-wider mb-1">
              <Send className="w-4 h-4" />
              <span>Telegram Private Storage Channel</span>
            </div>
            <h2 className="text-2xl font-bold text-white">Database Virtual Folders</h2>
            <p className="text-sm text-slate-400 mt-1 max-w-2xl">
              Telegram channels store files in a flat timeline. Virtual folders (e.g.{' '}
              <code className="text-indigo-300 bg-slate-800 px-1.5 py-0.5 rounded text-xs">anime/naruto/season-1</code>)
              are managed in the database metadata.
            </p>
          </div>

          <div className="flex items-center space-x-3">
            <button
              onClick={handleRefresh}
              className={`p-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-all ${
                isRefreshing ? 'animate-spin text-indigo-400' : ''
              }`}
              title="Refresh Folder List"
            >
              <RefreshCw className="w-5 h-5" />
            </button>
            <button
              onClick={() => setShowCreateModal(true)}
              className="flex items-center space-x-2 px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-sm shadow-lg shadow-indigo-600/30 transition-all"
            >
              <FolderPlus className="w-4 h-4" />
              <span>New Virtual Folder</span>
            </button>
          </div>
        </div>

        {/* Selected Folder Banner */}
        <div className="mt-6 pt-5 border-t border-slate-800/80 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div className="flex items-center space-x-3">
            <div className="p-2 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
              <Folder className="w-5 h-5" />
            </div>
            <div>
              <span className="text-xs text-slate-400 uppercase tracking-wider font-semibold block">
                Currently Selected Destination Virtual Folder
              </span>
              <span className="text-base font-mono font-bold text-amber-300">
                {selectedFolder ? `${selectedFolder}/` : 'No Folder Selected'}
              </span>
            </div>
          </div>

          {selectedFolder && (
            <button
              onClick={onNavigateToUpload}
              className="flex items-center space-x-2 px-3.5 py-1.5 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 text-xs font-semibold transition-all self-start sm:self-auto"
            >
              <UploadCloud className="w-4 h-4" />
              <span>Upload Files to Folder</span>
              <ArrowRight className="w-3.5 h-3.5 ml-1" />
            </button>
          )}
        </div>
      </div>

      {/* Search Bar */}
      <div className="relative">
        <Search className="w-5 h-5 absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
        <input
          type="text"
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          placeholder="Filter virtual folders by name or path..."
          className="w-full pl-11 pr-4 py-3 bg-slate-900 border border-slate-800 rounded-xl text-slate-200 placeholder-slate-500 text-sm focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
        />
      </div>

      {/* Folder Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {filteredFolders.map((folder) => {
          const isSelected = selectedFolder === folder.path;

          return (
            <div
              key={folder.path}
              onClick={() => onSelectFolder(folder.path)}
              className={`p-5 rounded-xl border transition-all cursor-pointer group relative ${
                isSelected
                  ? 'bg-indigo-950/40 border-indigo-500 shadow-lg shadow-indigo-500/10 ring-1 ring-indigo-500/50'
                  : 'bg-slate-900/60 border-slate-800 hover:border-slate-700 hover:bg-slate-900'
              }`}
            >
              <div className="flex items-start justify-between">
                <div className="flex items-center space-x-3">
                  <div className={`p-2.5 rounded-xl ${
                    isSelected 
                      ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30' 
                      : 'bg-slate-800 text-slate-300 group-hover:bg-slate-700 group-hover:text-white'
                  }`}>
                    <Folder className="w-5 h-5" />
                  </div>
                  <div>
                    <h3 className="font-mono font-bold text-sm text-white group-hover:text-indigo-300 transition-colors truncate max-w-[180px]">
                      {folder.name}
                    </h3>
                    <div className="flex items-center space-x-2 text-xs text-slate-400 mt-0.5">
                      <span className="flex items-center">
                        <FileVideo className="w-3.5 h-3.5 mr-1 text-slate-500" />
                        {folder.fileCount} files
                      </span>
                    </div>
                  </div>
                </div>

                {isSelected ? (
                  <span className="flex items-center space-x-1 px-2.5 py-1 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 text-[11px] font-semibold">
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    <span>Selected</span>
                  </span>
                ) : (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelectFolder(folder.path);
                    }}
                    className="opacity-0 group-hover:opacity-100 px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-indigo-600 text-slate-300 hover:text-white text-xs font-medium transition-all"
                  >
                    Select
                  </button>
                )}
              </div>

              <div className="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400 font-mono">
                <span>Path: {folder.path}/</span>
                <span>Updated: {folder.updatedAt}</span>
              </div>
            </div>
          );
        })}
      </div>

      {filteredFolders.length === 0 && (
        <div className="bg-slate-900/40 border border-dashed border-slate-800 rounded-2xl p-12 text-center">
          <Folder className="w-12 h-12 text-slate-600 mx-auto mb-3" />
          <h3 className="text-base font-semibold text-slate-300">No virtual folders found</h3>
          <p className="text-xs text-slate-500 mt-1">
            Create a new virtual folder path to organize Telegram storage messages.
          </p>
          <button
            onClick={() => setShowCreateModal(true)}
            className="mt-4 inline-flex items-center space-x-2 px-4 py-2 rounded-lg bg-indigo-600 text-white text-xs font-medium hover:bg-indigo-500 transition-all"
          >
            <FolderPlus className="w-4 h-4" />
            <span>Create New Folder</span>
          </button>
        </div>
      )}

      {/* Create Folder Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 backdrop-blur-sm p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 max-w-md w-full shadow-2xl space-y-4">
            <div className="flex items-center space-x-3">
              <div className="p-2.5 rounded-xl bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                <FolderPlus className="w-6 h-6" />
              </div>
              <div>
                <h3 className="text-lg font-bold text-white">Create Virtual Folder Path</h3>
                <p className="text-xs text-slate-400">Specify metadata folder path</p>
              </div>
            </div>

            <form onSubmit={handleCreate} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider text-slate-400 mb-1.5">
                  Folder Path (e.g. <span className="font-mono text-indigo-300">anime/naruto/season-1</span>)
                </label>
                <input
                  type="text"
                  required
                  value={newFolderName}
                  onChange={(e) => setNewFolderName(e.target.value)}
                  placeholder="e.g. anime/naruto/season-1"
                  className="w-full px-3.5 py-2.5 bg-slate-950 border border-slate-800 rounded-xl text-slate-100 text-sm font-mono focus:outline-none focus:border-indigo-500"
                />
                <p className="text-[11px] text-slate-400 mt-1">
                  Virtual folders are stored in the database metadata without altering the physical flat Telegram storage channel structure.
                </p>
              </div>

              <div className="flex items-center justify-end space-x-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="px-4 py-2 rounded-xl text-xs font-medium text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-2 rounded-xl text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-500 shadow-md shadow-indigo-600/30"
                >
                  Create &amp; Select
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
