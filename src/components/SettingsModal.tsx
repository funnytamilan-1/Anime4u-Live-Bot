import React, { useState } from 'react';
import { 
  Settings, 
  Database, 
  ShieldCheck, 
  Save, 
  Check, 
  Info, 
  Send
} from 'lucide-react';
import { AppConfig } from '../types';

interface SettingsModalProps {
  config: AppConfig;
  onSaveConfig: (newConfig: AppConfig) => void;
  onClose: () => void;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  config,
  onSaveConfig,
  onClose,
}) => {
  const [formData, setFormData] = useState<AppConfig>({ ...config });
  const [saved, setSaved] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSaveConfig(formData);
    setSaved(true);
    setTimeout(() => {
      setSaved(false);
      onClose();
    }, 1200);
  };

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-2xl space-y-6 my-4">
      <div className="flex items-center justify-between border-b border-slate-800 pb-4">
        <div className="flex items-center space-x-3">
          <div className="p-2.5 rounded-xl bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
            <Settings className="w-6 h-6" />
          </div>
          <div>
            <h3 className="text-xl font-bold text-white">Bot Credentials &amp; Telegram Channel Storage Config</h3>
            <p className="text-xs text-slate-400">
              Matches <code className="text-indigo-300">.env.example</code> (Zero Backblaze B2 dependencies)
            </p>
          </div>
        </div>
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-white text-sm px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700"
        >
          ✕
        </button>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5 text-xs">
        {/* Telegram Storage Channel Credentials */}
        <div className="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
          <h4 className="font-bold text-white text-sm flex items-center space-x-2">
            <Send className="w-4 h-4 text-cyan-400" />
            <span>Telegram Channel Storage Credentials</span>
          </h4>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 font-mono">
            <div>
              <label className="block text-slate-400 mb-1">BOT_TOKEN</label>
              <input
                type="text"
                value={formData.botToken}
                onChange={(e) => setFormData({ ...formData, botToken: e.target.value })}
                className="w-full px-3 py-2 bg-slate-900 border border-slate-800 rounded-lg text-slate-200 focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div>
              <label className="block text-slate-400 mb-1">STORAGE_CHANNEL_ID</label>
              <input
                type="text"
                value={formData.storageChannelId}
                onChange={(e) => setFormData({ ...formData, storageChannelId: e.target.value })}
                className="w-full px-3 py-2 bg-slate-900 border border-slate-800 rounded-lg text-slate-200 focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div>
              <label className="block text-slate-400 mb-1">ADMIN_IDS (comma-separated)</label>
              <input
                type="text"
                value={formData.adminIds}
                onChange={(e) => setFormData({ ...formData, adminIds: e.target.value })}
                className="w-full px-3 py-2 bg-slate-900 border border-slate-800 rounded-lg text-slate-200 focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div>
              <label className="block text-slate-400 mb-1">MAX_FILE_SIZE (Bytes)</label>
              <input
                type="number"
                value={formData.maxFileSize}
                onChange={(e) => setFormData({ ...formData, maxFileSize: parseInt(e.target.value, 10) })}
                className="w-full px-3 py-2 bg-slate-900 border border-slate-800 rounded-lg text-slate-200 focus:outline-none focus:border-indigo-500"
              />
            </div>
          </div>
        </div>

        {/* Database Credentials */}
        <div className="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
          <h4 className="font-bold text-white text-sm flex items-center space-x-2">
            <Database className="w-4 h-4 text-purple-400" />
            <span>Database Configuration (Supabase or SQLite)</span>
          </h4>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 font-mono">
            <div>
              <label className="block text-slate-400 mb-1">SUPABASE_URL</label>
              <input
                type="text"
                value={formData.supabaseUrl}
                onChange={(e) => setFormData({ ...formData, supabaseUrl: e.target.value })}
                className="w-full px-3 py-2 bg-slate-900 border border-slate-800 rounded-lg text-slate-200 focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div>
              <label className="block text-slate-400 mb-1">SUPABASE_SERVICE_ROLE_KEY</label>
              <input
                type="password"
                value={formData.supabaseRoleKey}
                onChange={(e) => setFormData({ ...formData, supabaseRoleKey: e.target.value })}
                className="w-full px-3 py-2 bg-slate-900 border border-slate-800 rounded-lg text-slate-200 focus:outline-none focus:border-indigo-500"
              />
            </div>
          </div>
        </div>

        {/* Action Footer */}
        <div className="flex items-center justify-between pt-3">
          <div className="text-[11px] text-slate-500 flex items-center space-x-1">
            <Info className="w-3.5 h-3.5" />
            <span>Settings saved for current bot session.</span>
          </div>

          <div className="flex items-center space-x-3">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-xl text-xs font-semibold text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex items-center space-x-2 px-5 py-2 rounded-xl text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-500 shadow-md shadow-indigo-600/30"
            >
              {saved ? <Check className="w-4 h-4" /> : <Save className="w-4 h-4" />}
              <span>{saved ? 'Saved!' : 'Save Settings'}</span>
            </button>
          </div>
        </div>
      </form>
    </div>
  );
};
