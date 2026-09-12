import React, { useState } from 'react';
import { 
  X, 
  Play, 
  Radio, 
  ExternalLink, 
  Copy, 
  Check, 
  Code, 
  Layers, 
  FileVideo, 
  Folder 
} from 'lucide-react';
import { StreamItem } from '../types';

interface VideoPlayerModalProps {
  stream: StreamItem;
  onClose: () => void;
}

export const VideoPlayerModal: React.FC<VideoPlayerModalProps> = ({ stream, onClose }) => {
  const [copied, setCopied] = useState(false);
  const [showCode, setShowCode] = useState(false);

  const copyUrl = () => {
    navigator.clipboard.writeText(stream.video_url);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const embedCode = `<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<video id="video" controls autoplay></video>
<script>
  var video = document.getElementById('video');
  var videoSrc = '${stream.video_url}';
  if (Hls.isSupported()) {
    var hls = new Hls();
    hls.loadSource(videoSrc);
    hls.attachMedia(video);
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = videoSrc;
  }
</script>`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/85 backdrop-blur-md p-4 overflow-y-auto">
      <div className="bg-slate-900 border border-slate-800 rounded-2xl max-w-3xl w-full shadow-2xl overflow-hidden flex flex-col my-8">
        {/* Modal Header */}
        <div className="p-5 border-b border-slate-800 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="p-2 rounded-xl bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
              <Play className="w-5 h-5 fill-current" />
            </div>
            <div>
              <h3 className="font-bold text-white text-lg leading-tight">{stream.title}</h3>
              <p className="text-xs text-slate-400 font-mono">
                {stream.folder_path ? `${stream.folder_path}/` : 'Telegram Channel Storage'}
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white transition-all"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Video Player Display */}
        <div className="bg-black aspect-video relative flex items-center justify-center group">
          <video
            src={stream.video_url}
            controls
            autoPlay
            className="w-full h-full object-contain"
            poster="https://images.unsplash.com/photo-1578632767115-351597cf2477?w=1200&auto=format&fit=crop&q=80"
          >
            Your browser does not support the video tag.
          </video>
        </div>

        {/* Details & Controls */}
        <div className="p-6 space-y-4 bg-slate-900">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs">
            <div className="flex items-center space-x-2">
              <span className={`px-2.5 py-1 rounded-full text-[11px] font-bold uppercase tracking-wider ${
                stream.is_live
                  ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30'
                  : 'bg-slate-800 text-slate-400 border border-slate-700'
              }`}>
                {stream.is_live ? '• LIVE STREAM' : 'VOD ARCHIVE'}
              </span>
              <span className="text-slate-400 font-mono">ID: {stream.id}</span>
            </div>

            <div className="flex items-center space-x-2">
              <button
                onClick={() => setShowCode(!showCode)}
                className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white font-medium flex items-center space-x-1"
              >
                <Code className="w-3.5 h-3.5" />
                <span>{showCode ? 'Hide Embed Code' : 'Get HLS Code'}</span>
              </button>
              <button
                onClick={copyUrl}
                className="px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-semibold flex items-center space-x-1 shadow-md shadow-indigo-600/30"
              >
                {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                <span>{copied ? 'Copied URL' : 'Copy HLS URL'}</span>
              </button>
            </div>
          </div>

          {/* Embed Code Snippet */}
          {showCode && (
            <div className="bg-slate-950 p-4 rounded-xl border border-slate-800 font-mono text-xs space-y-2">
              <div className="flex items-center justify-between text-slate-400 text-[11px]">
                <span>HLS.js Integration Code</span>
                <button
                  onClick={() => navigator.clipboard.writeText(embedCode)}
                  className="text-indigo-400 hover:underline"
                >
                  Copy HTML
                </button>
              </div>
              <pre className="text-slate-300 whitespace-pre-wrap overflow-x-auto text-[11px] leading-relaxed">
                {embedCode}
              </pre>
            </div>
          )}

          <div className="bg-slate-950 p-3 rounded-xl border border-slate-800 text-xs font-mono text-slate-400 flex items-center justify-between truncate">
            <span className="truncate pr-2">{stream.video_url}</span>
            <a
              href={stream.video_url}
              target="_blank"
              rel="noreferrer"
              className="text-indigo-400 hover:text-indigo-300 shrink-0"
            >
              <ExternalLink className="w-4 h-4" />
            </a>
          </div>
        </div>
      </div>
    </div>
  );
};
