import React from 'react';
import { 
  Play, 
  Folder, 
  Upload, 
  MessageSquare, 
  Settings, 
  CheckCircle2, 
  Radio, 
  Server,
  ShieldCheck,
  Send
} from 'lucide-react';
import { AppConfig } from '../types';

interface HeaderProps {
  activeTab: 'streams' | 'uploader' | 'folders' | 'bot' | 'settings';
  setActiveTab: (tab: 'streams' | 'uploader' | 'folders' | 'bot' | 'settings') => void;
  config: AppConfig;
  selectedFolder: string;
  streamCount: number;
}

export const Header: React.FC<HeaderProps> = ({
  activeTab,
  setActiveTab,
  config,
  selectedFolder,
  streamCount,
}) => {
  return (
    <header className="bg-slate-900/80 backdrop-blur-md border-b border-slate-800 sticky top-0 z-30">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          {/* Logo & Brand */}
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 via-purple-500 to-pink-500 p-0.5 flex items-center justify-center shadow-lg shadow-indigo-500/20">
              <div className="w-full h-full bg-slate-950 rounded-[10px] flex items-center justify-center">
                <Send className="w-5 h-5 text-indigo-400" />
              </div>
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h1 className="text-lg font-bold text-white tracking-wide">Anime4u Storage Bot</h1>
                <span className="bg-indigo-500/10 text-indigo-400 border border-indigo-500/30 text-[10px] font-semibold px-2 py-0.5 rounded-full uppercase tracking-wider">
                  Telegram Channel Storage
                </span>
              </div>
              <p className="text-xs text-slate-400 hidden sm:block">
                Private Channel Storage &amp; Virtual Folder Metadata
              </p>
            </div>
          </div>

          {/* Quick Connection Badges */}
          <div className="hidden lg:flex items-center space-x-4 text-xs">
            <div className="flex items-center space-x-1.5 px-2.5 py-1 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
              <CheckCircle2 className="w-3.5 h-3.5" />
              <span className="font-medium">Storage Channel:</span>
              <span className="text-slate-300 font-mono">{config.storageChannelId || '-1001234567890'}</span>
            </div>
            <div className="flex items-center space-x-1.5 px-2.5 py-1 rounded-lg bg-purple-500/10 text-purple-400 border border-purple-500/20">
              <Server className="w-3.5 h-3.5" />
              <span className="font-medium">Indexed Files:</span>
              <span className="text-slate-200 font-semibold">{streamCount}</span>
            </div>
            {selectedFolder && (
              <div className="flex items-center space-x-1.5 px-2.5 py-1 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
                <Folder className="w-3.5 h-3.5" />
                <span className="font-medium">Folder:</span>
                <span className="text-slate-200 font-mono font-medium truncate max-w-[140px]">
                  {selectedFolder}/
                </span>
              </div>
            )}
          </div>

          {/* Navigation Tabs */}
          <nav className="flex items-center space-x-1 sm:space-x-2">
            <button
              onClick={() => setActiveTab('streams')}
              className={`flex items-center space-x-2 px-3 py-2 rounded-lg text-xs sm:text-sm font-medium transition-all ${
                activeTab === 'streams'
                  ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              <Radio className="w-4 h-4" />
              <span className="hidden md:inline">Storage Files</span>
              <span className="md:hidden">Files</span>
            </button>

            <button
              onClick={() => setActiveTab('uploader')}
              className={`flex items-center space-x-2 px-3 py-2 rounded-lg text-xs sm:text-sm font-medium transition-all ${
                activeTab === 'uploader'
                  ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              <Upload className="w-4 h-4" />
              <span>Direct Storage Upload</span>
            </button>

            <button
              onClick={() => setActiveTab('folders')}
              className={`flex items-center space-x-2 px-3 py-2 rounded-lg text-xs sm:text-sm font-medium transition-all ${
                activeTab === 'folders'
                  ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              <Folder className="w-4 h-4" />
              <span className="hidden sm:inline">Virtual Folders</span>
            </button>

            <button
              onClick={() => setActiveTab('bot')}
              className={`flex items-center space-x-2 px-3 py-2 rounded-lg text-xs sm:text-sm font-medium transition-all ${
                activeTab === 'bot'
                  ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              <MessageSquare className="w-4 h-4 text-cyan-400" />
              <span className="hidden sm:inline">Bot Console</span>
            </button>

            <button
              onClick={() => setActiveTab('settings')}
              className={`p-2 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 transition-all ${
                activeTab === 'settings' ? 'text-white bg-slate-800' : ''
              }`}
              title="Settings & Credentials"
            >
              <Settings className="w-4 h-4" />
            </button>
          </nav>
        </div>
      </div>
    </header>
  );
};
