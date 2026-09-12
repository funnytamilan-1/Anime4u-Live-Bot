import React, { useState } from 'react';
import { 
  Play, 
  Search, 
  Trash2, 
  Folder, 
  Send, 
  Copy, 
  Check, 
  ExternalLink, 
  FileVideo, 
  Tag, 
  MoreVertical,
  Plus,
  RefreshCw,
  FolderInput,
  Radio
} from 'lucide-react';
import { StreamItem } from '../types';

interface StreamListProps {
  streams: StreamItem[];
  onToggleLive: (id: string) => void;
  onDeleteStream: (id: string) => void;
  onAddStream: (stream: StreamItem) => void;
  onPlayStream: (stream: StreamItem) => void;
}

export const StreamList: React.FC<StreamListProps> = ({
  streams,
  onToggleLive,
  onDeleteStream,
  onAddStream,
  onPlayStream,
}) => {
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedFolderFilter, setSelectedFolderFilter] = useState('ALL');
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [editingMoveStream, setEditingMoveStream] = useState<StreamItem | null>(null);
  const [newFolderPathInput, setNewFolderPathInput] = useState('');

  // Get unique folder list
  const folderList = Array.from(new Set(streams.map((s) => s.folder_path || 'root')));

  const filteredStreams = streams.filter((s) => {
    const matchesSearch =
      s.title.toLowerCase().includes(searchTerm.toLowerCase()) ||
      s.file_name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      s.folder_path.toLowerCase().includes(searchTerm.toLowerCase());

    const matchesFolder =
      selectedFolderFilter === 'ALL' || s.folder_path === selectedFolderFilter;

    return matchesSearch && matchesFolder;
  });

  const copyText = (text: string, id: string) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const handleMoveFolderSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingMoveStream || !newFolderPathInput.trim()) return;

    editingMoveStream.folder_path = newFolderPathInput.trim();
    setEditingMoveStream(null);
    setNewFolderPathInput('');
  };

  return (
    <div className="space-y-6">
      {/* Header Banner */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <div className="flex items-center space-x-2 text-indigo-400 text-xs font-semibold uppercase tracking-wider mb-1">
            <Radio className="w-4 h-4" />
            <span>Database Indexing &amp; Storage Records</span>
          </div>
          <h2 className="text-2xl font-bold text-white">Indexed Storage Channel Files</h2>
          <p className="text-xs text-slate-400 mt-1">
            All stored media files indexed with Telegram message IDs and virtual folder metadata.
          </p>
        </div>

        <div className="flex items-center space-x-3 text-xs">
          <div className="bg-slate-950 px-3 py-2 rounded-xl border border-slate-800 font-mono">
            <span className="text-slate-400">Total Indexed: </span>
            <span className="text-indigo-400 font-bold">{streams.length}</span>
          </div>
        </div>
      </div>

      {/* Filter & Search Bar */}
      <div className="flex flex-col md:flex-row gap-3">
        {/* Search Input */}
        <div className="relative flex-1">
          <Search className="w-4 h-4 absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            placeholder="Search by title, filename, or folder path..."
            className="w-full pl-10 pr-4 py-2.5 bg-slate-900 border border-slate-800 rounded-xl text-slate-200 placeholder-slate-500 text-xs focus:outline-none focus:border-indigo-500"
          />
        </div>

        {/* Folder Dropdown Filter */}
        <div className="flex items-center space-x-2">
          <Folder className="w-4 h-4 text-amber-400 shrink-0" />
          <select
            value={selectedFolderFilter}
            onChange={(e) => setSelectedFolderFilter(e.target.value)}
            className="px-3 py-2.5 bg-slate-900 border border-slate-800 rounded-xl text-slate-200 text-xs font-mono focus:outline-none focus:border-indigo-500"
          >
            <option value="ALL">All Virtual Folders</option>
            {folderList.map((f) => (
              <option key={f} value={f}>
                {f}/
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Stream Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {filteredStreams.map((stream) => (
          <div
            key={stream.id}
            className="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 hover:border-slate-700 transition-all flex flex-col justify-between group space-y-4 shadow-lg shadow-black/20"
          >
            {/* Top Info */}
            <div className="space-y-3">
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center space-x-2">
                  <div className="p-2 rounded-xl bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                    <FileVideo className="w-5 h-5" />
                  </div>
                  <div>
                    <span className="text-[11px] font-mono text-amber-400 flex items-center space-x-1">
                      <Folder className="w-3 h-3" />
                      <span>{stream.folder_path}/</span>
                    </span>
                    <h3 className="font-bold text-white text-sm line-clamp-1 group-hover:text-indigo-300 transition-colors">
                      {stream.title}
                    </h3>
                  </div>
                </div>

                <button
                  onClick={() => onPlayStream(stream)}
                  className="p-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white shadow-md shadow-indigo-600/30 transition-all shrink-0"
                  title="Play / View Stream"
                >
                  <Play className="w-4 h-4 fill-current" />
                </button>
              </div>

              {/* Metadata Badges */}
              <div className="grid grid-cols-2 gap-2 font-mono text-[11px]">
                <div className="bg-slate-950 p-2 rounded-lg border border-slate-800">
                  <span className="text-slate-500 block text-[10px]">Storage Msg ID</span>
                  <span className="text-indigo-400 font-bold">{stream.storage_message_id}</span>
                </div>
                <div className="bg-slate-950 p-2 rounded-lg border border-slate-800">
                  <span className="text-slate-500 block text-[10px]">File Size</span>
                  <span className="text-slate-300">{stream.file_size || '350 MB'}</span>
                </div>
              </div>

              <div className="text-[11px] font-mono text-slate-400 bg-slate-950 p-2 rounded-lg border border-slate-800/80 truncate">
                Filename: <span className="text-slate-200">{stream.file_name}</span>
              </div>
            </div>

            {/* Actions Bar */}
            <div className="pt-3 border-t border-slate-800/80 flex items-center justify-between text-xs">
              <button
                onClick={() => {
                  setEditingMoveStream(stream);
                  setNewFolderPathInput(stream.folder_path);
                }}
                className="flex items-center space-x-1 text-slate-400 hover:text-indigo-300 transition-colors"
              >
                <FolderInput className="w-3.5 h-3.5" />
                <span>Move Folder</span>
              </button>

              <div className="flex items-center space-x-2">
                <button
                  onClick={() => copyText(stream.video_url, stream.id)}
                  className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition-all"
                  title="Copy Stream URL"
                >
                  {copiedId === stream.id ? (
                    <Check className="w-3.5 h-3.5 text-emerald-400" />
                  ) : (
                    <Copy className="w-3.5 h-3.5" />
                  )}
                </button>

                <button
                  onClick={() => {
                    if (confirm(`Delete storage record for message #${stream.storage_message_id}?`)) {
                      onDeleteStream(stream.id);
                    }
                  }}
                  className="p-1.5 rounded-lg bg-slate-800 hover:bg-rose-950 hover:text-rose-400 text-slate-400 transition-all"
                  title="Delete File Record"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>

      {filteredStreams.length === 0 && (
        <div className="bg-slate-900/40 border border-dashed border-slate-800 rounded-2xl p-12 text-center">
          <Send className="w-12 h-12 text-slate-600 mx-auto mb-3" />
          <h3 className="text-base font-semibold text-slate-300">No storage records match search</h3>
          <p className="text-xs text-slate-500 mt-1">
            Try adjusting filters or upload new files into virtual folders.
          </p>
        </div>
      )}

      {/* Move Folder Modal */}
      {editingMoveStream && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 backdrop-blur-sm p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 max-w-md w-full shadow-2xl space-y-4">
            <div className="flex items-center space-x-3">
              <div className="p-2.5 rounded-xl bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                <FolderInput className="w-6 h-6" />
              </div>
              <div>
                <h3 className="text-lg font-bold text-white">Move Virtual Folder</h3>
                <p className="text-xs text-slate-400">Updates database metadata without re-uploading file</p>
              </div>
            </div>

            <form onSubmit={handleMoveFolderSubmit} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider text-slate-400 mb-1">
                  New Virtual Folder Path
                </label>
                <input
                  type="text"
                  required
                  value={newFolderPathInput}
                  onChange={(e) => setNewFolderPathInput(e.target.value)}
                  className="w-full px-3.5 py-2.5 bg-slate-950 border border-slate-800 rounded-xl text-slate-100 text-sm font-mono focus:outline-none focus:border-indigo-500"
                />
              </div>

              <div className="flex items-center justify-end space-x-3 pt-2">
                <button
                  type="button"
                  onClick={() => setEditingMoveStream(null)}
                  className="px-4 py-2 rounded-xl text-xs font-medium text-slate-400 hover:text-white bg-slate-800"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-2 rounded-xl text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-500"
                >
                  Save Folder Move
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
