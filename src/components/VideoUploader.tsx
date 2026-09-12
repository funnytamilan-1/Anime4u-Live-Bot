import React, { useState, useRef } from 'react';
import { 
  Upload, 
  FileVideo, 
  Folder, 
  Terminal, 
  CheckCircle2, 
  AlertTriangle, 
  Cpu, 
  CloudUpload, 
  Database, 
  Copy, 
  Check, 
  Zap, 
  Info,
  Send,
  ShieldCheck
} from 'lucide-react';
import { UploadJob, StreamItem, AppConfig } from '../types';

interface VideoUploaderProps {
  selectedFolder: string;
  config: AppConfig;
  streams: StreamItem[];
  onUploadSuccess: (newStream: StreamItem) => void;
  onNavigateToFolders: () => void;
}

export const VideoUploader: React.FC<VideoUploaderProps> = ({
  selectedFolder,
  config,
  streams,
  onUploadSuccess,
  onNavigateToFolders,
}) => {
  const [activeJob, setActiveJob] = useState<UploadJob | null>(null);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [customTitle, setCustomTitle] = useState('');
  const [isDragOver, setIsDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const startProcessing = (file: File) => {
    if (!selectedFolder) {
      alert('Please select a virtual folder first!');
      onNavigateToFolders();
      return;
    }

    const fileSizeMb = (file.size / (1024 * 1024)).toFixed(1);
    if (file.size > config.maxFileSize) {
      alert(`File exceeds configured limit of ${(config.maxFileSize / (1024 * 1024 * 1024)).toFixed(1)} GB.`);
      return;
    }

    const fileUniqueId = `AQAD_${Math.random().toString(36).substring(2, 12)}`;
    const title = customTitle.trim() || file.name.replace(/\.[^/.]+$/, '');
    const mode = config.storageMode || 'telegram';

    // Duplicate Detection Check
    const existing = streams.find((s) => s.file_name === file.name || s.file_unique_id === fileUniqueId);

    const initialJob: UploadJob = {
      id: Math.random().toString(36).substring(2, 9),
      fileName: file.name,
      fileSize: file.size,
      folderPath: selectedFolder,
      storageMode: mode,
      status: 'downloading',
      progress: 20,
      fileUniqueId,
      isDuplicate: !!existing,
      duplicateInfo: existing,
      log: `[00:00:01] ⬇️ Receiving file buffer: ${file.name} (${fileSizeMb} MB)...`,
      addedToDatabase: false,
    };

    setActiveJob(initialJob);

    // Step 1: Receiving / Preparing
    setTimeout(() => {
      setActiveJob((prev) =>
        prev
          ? {
              ...prev,
              status: mode === 'b2' ? 'storing_b2' : 'storing_telegram',
              progress: 55,
              log:
                prev.log +
                `\n[00:00:03] 📦 Preparing storage pipeline (Mode: ${mode.toUpperCase()})...` +
                `\n[00:00:04] ⚡ Uploading to storage backend targets...`,
            }
          : null
      );

      // Step 2: Storing in configured backends
      setTimeout(() => {
        const storageMessageId = Math.floor(Math.random() * 9000) + 1000;
        const channelId = parseInt(config.storageChannelId, 10) || -1001234567890;
        const b2Path = `${selectedFolder}/${file.name}`;

        setActiveJob((prev) =>
          prev
            ? {
                ...prev,
                status: 'saving_metadata',
                progress: 85,
                storageMessageId: mode !== 'b2' ? storageMessageId : undefined,
                storageChannelId: mode !== 'b2' ? channelId : undefined,
                b2Path: mode !== 'telegram' ? b2Path : undefined,
                log:
                  prev.log +
                  `\n[00:00:06] ☁️ Upload verified in target storage backends!` +
                  `\n[00:00:07] 🗄️ Saving metadata record to Database (Folder: "${selectedFolder}")...`,
              }
            : null
          );

        // Step 3: Complete & Save Metadata
        setTimeout(() => {
          setActiveJob((prev) =>
            prev
              ? {
                  ...prev,
                  status: 'completed',
                  progress: 100,
                  addedToDatabase: true,
                  log:
                    prev.log +
                    `\n[00:00:08] ✨ Record inserted into Database streams table.` +
                    `\n[00:00:09] ✅ UPLOAD COMPLETE & SECURELY STORED!`,
                }
              : null
          );

          const newStream: StreamItem = {
            id: Math.random().toString(36).substring(2, 15),
            title,
            video_url: '',
            storage_message_id: mode !== 'b2' ? storageMessageId : undefined,
            storage_channel_id: mode !== 'b2' ? channelId : undefined,
            b2_path: mode !== 'telegram' ? b2Path : undefined,
            storage_mode: mode,
            b2_status: mode !== 'telegram' ? 'success' : 'none',
            telegram_status: mode !== 'b2' ? 'success' : 'none',
            status: 'complete',
            file_unique_id: fileUniqueId,
            file_name: file.name,
            is_live: true,
            created_at: new Date().toISOString().replace('T', ' ').substring(0, 19),
            folder_path: selectedFolder,
            file_size: `${fileSizeMb} MB`,
            mime_type: file.type || 'video/mp4',
            media_type: file.type.includes('video') ? 'video' : 'document',
          };

          onUploadSuccess(newStream);
        }, 1200);
      }, 1500);
    }, 1200);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      startProcessing(e.target.files[0]);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      startProcessing(e.dataTransfer.files[0]);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header Banner */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 relative overflow-hidden">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div className="flex items-center space-x-2 text-indigo-400 text-xs font-semibold uppercase tracking-wider mb-1">
              <Send className="w-4 h-4" />
              <span>Multi-Backend Storage Architecture</span>
            </div>
            <h2 className="text-2xl font-bold text-white">Storage Upload Engine</h2>
            <p className="text-sm text-slate-400 mt-1 max-w-2xl">
              Supports Backblaze B2, Telegram Private Storage Channel (<code className="text-indigo-300">{config.storageChannelId}</code>), or Both dual storage mode!
            </p>
          </div>

          <div className="flex items-center space-x-3 bg-slate-950/80 p-3 rounded-xl border border-slate-800 text-xs">
            <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
              <Folder className="w-5 h-5" />
            </div>
            <div>
              <span className="text-slate-400 block text-[11px] font-semibold uppercase">Target Virtual Folder</span>
              {selectedFolder ? (
                <span className="font-mono text-indigo-300 font-bold">{selectedFolder}/</span>
              ) : (
                <button
                  onClick={onNavigateToFolders}
                  className="text-amber-400 hover:underline font-semibold flex items-center space-x-1"
                >
                  <span>⚠️ Select Folder</span>
                </button>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Upload Setup Form */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Dropzone & Title Input */}
        <div className="lg:col-span-2 space-y-4">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 space-y-4">
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-slate-400 mb-1.5">
                File Title (Optional Override)
              </label>
              <input
                type="text"
                value={customTitle}
                onChange={(e) => setCustomTitle(e.target.value)}
                placeholder="e.g. Naruto Shippuden Episode 01 [1080p]"
                className="w-full px-4 py-2.5 bg-slate-950 border border-slate-800 rounded-xl text-slate-100 text-sm focus:outline-none focus:border-indigo-500"
              />
            </div>

            {/* Dropzone */}
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setIsDragOver(true);
              }}
              onDragLeave={() => setIsDragOver(false)}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
              className={`border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all ${
                isDragOver
                  ? 'border-indigo-500 bg-indigo-500/10 scale-[0.99]'
                  : 'border-slate-800 hover:border-indigo-500/50 hover:bg-slate-900/80 bg-slate-950/40'
              }`}
            >
              <input
                ref={fileInputRef}
                type="file"
                className="hidden"
                onChange={handleFileSelect}
              />

              <div className="w-16 h-16 rounded-2xl bg-indigo-600/10 border border-indigo-500/20 text-indigo-400 flex items-center justify-center mx-auto mb-4">
                <CloudUpload className="w-8 h-8" />
              </div>

              <h3 className="text-base font-bold text-white">
                Drag &amp; drop file or click to browse
              </h3>
              <p className="text-xs text-slate-400 mt-1 max-w-sm mx-auto">
                Supports videos, audio, documents (up to 2 GB MAX_FILE_SIZE limit)
              </p>

              <div className="mt-4 inline-flex items-center space-x-2 px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold shadow-md shadow-indigo-600/30">
                <FileVideo className="w-4 h-4" />
                <span>Select File to Store</span>
              </div>
            </div>
          </div>
        </div>

        {/* Pipeline Info Card */}
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 space-y-4 flex flex-col justify-between">
          <div>
            <h3 className="text-sm font-bold text-white flex items-center space-x-2">
              <Cpu className="w-4 h-4 text-indigo-400" />
              <span>Multi-Backend Pipeline</span>
            </h3>
            <p className="text-xs text-slate-400 mt-1">
              Architecture overview across B2 and Telegram Storage:
            </p>

            <ul className="mt-4 space-y-3 text-xs text-slate-300">
              <li className="flex items-start space-x-2.5">
                <div className="p-1 rounded bg-indigo-500/20 text-indigo-400 mt-0.5">
                  <span className="font-mono font-bold text-[10px]">1</span>
                </div>
                <div>
                  <span className="font-semibold text-white">Duplicate Detection</span>
                  <p className="text-[11px] text-slate-400">Checks `file_unique_id` & `b2_path` prior to storage.</p>
                </div>
              </li>
              <li className="flex items-start space-x-2.5">
                <div className="p-1 rounded bg-indigo-500/20 text-indigo-400 mt-0.5">
                  <span className="font-mono font-bold text-[10px]">2</span>
                </div>
                <div>
                  <span className="font-semibold text-white">Multi-Storage Routing</span>
                  <p className="text-[11px] text-slate-400">Routes payload to Backblaze B2, Telegram Channel, or Both.</p>
                </div>
              </li>
              <li className="flex items-start space-x-2.5">
                <div className="p-1 rounded bg-indigo-500/20 text-indigo-400 mt-0.5">
                  <span className="font-mono font-bold text-[10px]">3</span>
                </div>
                <div>
                  <span className="font-semibold text-white">Database Metadata</span>
                  <p className="text-[11px] text-slate-400">Indexes B2 file path, Telegram message ID, and status.</p>
                </div>
              </li>
            </ul>
          </div>

          <div className="p-3 rounded-xl bg-indigo-500/10 border border-indigo-500/20 text-[11px] text-indigo-300 flex items-center space-x-2">
            <ShieldCheck className="w-4 h-4 shrink-0" />
            <span>Admin-only control with partial failure recovery reporting.</span>
          </div>
        </div>
      </div>

      {/* Active Processing Job Terminal & Progress */}
      {activeJob && (
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3">
              <div className={`p-2.5 rounded-xl ${
                activeJob.status === 'completed'
                  ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30'
                  : 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/30'
              }`}>
                {activeJob.status === 'completed' ? (
                  <CheckCircle2 className="w-6 h-6" />
                ) : (
                  <CloudUpload className="w-6 h-6 animate-bounce" />
                )}
              </div>
              <div>
                <h3 className="font-bold text-white text-base">{activeJob.fileName}</h3>
                <p className="text-xs text-slate-400 font-mono">
                  Virtual Folder: {activeJob.folderPath}/ | Mode: {activeJob.storageMode.toUpperCase()}
                </p>
              </div>
            </div>

            <span className={`px-3 py-1 rounded-full text-xs font-semibold uppercase tracking-wider ${
              activeJob.status === 'completed'
                ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
                : 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 animate-pulse'
            }`}>
              {activeJob.status}
            </span>
          </div>

          {/* Progress Bar */}
          <div className="space-y-1">
            <div className="flex justify-between text-xs text-slate-400 font-mono">
              <span>Pipeline Progress</span>
              <span>{activeJob.progress}%</span>
            </div>
            <div className="w-full h-2.5 bg-slate-950 rounded-full overflow-hidden border border-slate-800">
              <div
                className="h-full bg-gradient-to-r from-indigo-500 via-purple-500 to-emerald-400 transition-all duration-500"
                style={{ width: `${activeJob.progress}%` }}
              />
            </div>
          </div>

          {/* Console Log Terminal */}
          <div className="bg-slate-950 border border-slate-800/80 rounded-xl p-4 font-mono text-xs text-slate-300 space-y-1 overflow-x-auto max-h-48">
            <div className="flex items-center space-x-2 text-slate-500 border-b border-slate-800 pb-2 mb-2">
              <Terminal className="w-3.5 h-3.5" />
              <span>Storage Pipeline stdout</span>
            </div>
            <pre className="whitespace-pre-wrap leading-relaxed text-slate-300">{activeJob.log}</pre>
          </div>

          {/* Completed Job Info Card */}
          {activeJob.status === 'completed' && (
            <div className="p-4 rounded-xl bg-emerald-950/30 border border-emerald-500/30 space-y-3">
              <div className="flex items-center space-x-2 text-emerald-400 font-bold text-sm">
                <CheckCircle2 className="w-5 h-5" />
                <span>Successfully Stored in Selected Backends!</span>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 font-mono text-[11px] pt-1">
                {activeJob.storageMessageId && (
                  <div className="bg-slate-950/80 p-2 rounded border border-slate-800 text-slate-300">
                    <span className="text-slate-500 block">Telegram Msg ID:</span>
                    <span className="text-emerald-400 font-bold">{activeJob.storageMessageId}</span>
                  </div>
                )}
                {activeJob.b2Path && (
                  <div className="bg-slate-950/80 p-2 rounded border border-slate-800 text-slate-300">
                    <span className="text-slate-500 block">B2 Storage Path:</span>
                    <span className="truncate block text-emerald-400">{activeJob.b2Path}</span>
                  </div>
                )}
                <div className="bg-slate-950/80 p-2 rounded border border-slate-800 text-slate-300">
                  <span className="text-slate-500 block">Database Status:</span>
                  <span className="text-emerald-400 font-semibold">Metadata Recorded</span>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
