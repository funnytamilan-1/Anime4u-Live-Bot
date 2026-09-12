import React, { useState } from 'react';
import { 
  Send, 
  Terminal, 
  MessageSquare, 
  Folder, 
  Search, 
  Upload, 
  Info, 
  ShieldCheck, 
  RefreshCw,
  FileVideo,
  Bot
} from 'lucide-react';
import { StorageFolder, AppConfig, BotMessage } from '../types';

interface BotSimulatorProps {
  folders: StorageFolder[];
  selectedFolder: string;
  onSelectFolder: (folderPath: string) => void;
  onCreateFolder: (newFolderPath: string) => void;
  config: AppConfig;
}

export const BotSimulator: React.FC<BotSimulatorProps> = ({
  folders,
  selectedFolder,
  onSelectFolder,
  onCreateFolder,
  config,
}) => {
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<BotMessage[]>([
    {
      id: '1',
      sender: 'bot',
      text:
        `👑 *Anime4u Telegram Storage Manager*\n\n` +
        `Backend Storage Channel: \`${config.storageChannelId}\`\n` +
        `Active Virtual Folder: \`${selectedFolder || 'None'}/\`\n\n` +
        `Send any command or file to test storage commands.`,
      timestamp: '12:00 PM',
      buttons: [
        [
          { text: '📊 Status', action: 'cmd_status' },
          { text: '📂 Folders', action: 'cmd_folders' },
        ],
        [
          { text: '📤 Upload File', action: 'cmd_upload' },
          { text: '📁 New Folder', action: 'cmd_newfolder' },
        ],
        [
          { text: '🔎 Search', action: 'cmd_search' },
          { text: '⚙️ Settings', action: 'cmd_settings' },
        ],
      ],
    },
  ]);

  const addMessage = (sender: 'user' | 'bot', text: string, buttons?: { text: string; action: string }[][]) => {
    const newMsg: BotMessage = {
      id: Math.random().toString(36).substring(2, 9),
      sender,
      text,
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      buttons,
    };
    setMessages((prev) => [...prev, newMsg]);
  };

  const handleSend = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!input.trim()) return;

    const userText = input.trim();
    setInput('');
    addMessage('user', userText);

    setTimeout(() => {
      processCommand(userText);
    }, 400);
  };

  const handleButtonClick = (action: string, btnText: string) => {
    addMessage('user', btnText);
    setTimeout(() => {
      if (action === 'cmd_folders') {
        processCommand('/folders');
      } else if (action === 'cmd_newfolder') {
        processCommand('/newfolder');
      } else if (action === 'cmd_search') {
        processCommand('/search');
      } else if (action === 'cmd_status') {
        addMessage(
          'bot',
          `📊 *Telegram Storage Channel Status*\n\n` +
            `Channel ID: \`${config.storageChannelId}\`\n` +
            `Admin IDs: \`${config.adminIds}\`\n` +
            `Max File Limit: \`2.0 GB\`\n` +
            `Auto HLS Transcode: \`${config.autoHls ? 'ENABLED' : 'DISABLED'}\``
        );
      } else if (action.startsWith('select_folder:')) {
        const folderPath = action.replace('select_folder:', '');
        onSelectFolder(folderPath);
        addMessage(
          'bot',
          `✅ *Folder Selected!*\n\n📁 \`${folderPath}/\`\n\nNow send any video or file to upload.`
        );
      } else {
        processCommand('/start');
      }
    }, 400);
  };

  const processCommand = (cmd: string) => {
    const lower = cmd.toLowerCase().trim();

    if (lower === '/start') {
      addMessage(
        'bot',
        `👑 *Anime4u Telegram Storage Manager*\n\n` +
          `Backend Storage Channel: \`${config.storageChannelId}\`\n` +
          `Active Folder: \`${selectedFolder || 'None'}/\`\n\n` +
          `Send commands or files to manage channel storage.`,
        [
          [
            { text: '📊 Status', action: 'cmd_status' },
            { text: '📂 Folders', action: 'cmd_folders' },
          ],
          [
            { text: '📤 Upload File', action: 'cmd_upload' },
            { text: '📁 New Folder', action: 'cmd_newfolder' },
          ],
        ]
      );
    } else if (lower === '/folders') {
      const folderButtons = folders.map((f) => [
        { text: `📁 ${f.name} (${f.fileCount})`, action: `select_folder:${f.path}` },
      ]);
      addMessage(
        'bot',
        `📂 *Database Virtual Folders*\nSelect target folder:`,
        folderButtons
      );
    } else if (lower === '/newfolder') {
      addMessage(
        'bot',
        `📁 *New Folder Path*\n\nSend virtual folder path.\n\nExample:\n\`anime/naruto/season-1\``
      );
    } else if (lower === '/selected') {
      addMessage(
        'bot',
        `📁 *Currently Selected Folder:*\n\`${selectedFolder || 'None'}/\``
      );
    } else if (lower === '/cancel') {
      addMessage('bot', `❌ Current operation cancelled.`);
    } else if (lower.startsWith('/search')) {
      const q = lower.replace('/search', '').trim() || 'naruto';
      addMessage(
        'bot',
        `🔎 *Search Results for '${q}'*:\n\n` +
          `📄 Naruto Episode 01.mkv\n📁 anime/naruto/season-1\n🆔 Storage Msg ID: \`1001\`\n\n` +
          `📄 Naruto Episode 02.mkv\n📁 anime/naruto/season-1\n🆔 Storage Msg ID: \`1002\``
      );
    } else if (lower.includes('/') && !lower.startsWith('/')) {
      // New folder creation path input
      onCreateFolder(lower);
      onSelectFolder(lower);
      addMessage(
        'bot',
        `✅ *Folder Created & Selected!*\n\n📁 \`${lower}/\`\n\nSend any file or video now.`
      );
    } else {
      // Simulated upload
      if (!selectedFolder) {
        addMessage('bot', `📂 No folder selected. Use /folders or /newfolder first.`);
        return;
      }
      const msgId = Math.floor(Math.random() * 9000) + 1000;
      addMessage(
        'bot',
        `⬇️ Receiving file...\n📦 Preparing storage...\n☁️ Storing in Telegram Channel...\n🗄️ Saving metadata...\n\n` +
          `✅ *UPLOAD COMPLETE*\n\n` +
          `📄 File: \`${cmd}\`\n` +
          `📁 Folder: \`${selectedFolder}/\`\n` +
          `🆔 Storage Msg ID: \`${msgId}\`\n` +
          `Stored securely in Channel \`${config.storageChannelId}\`.`
      );
    }
  };

  return (
    <div className="space-y-6">
      {/* Banner */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <div className="flex items-center space-x-2 text-cyan-400 text-xs font-semibold uppercase tracking-wider mb-1">
            <Bot className="w-4 h-4" />
            <span>Interactive Telegram Bot Console</span>
          </div>
          <h2 className="text-2xl font-bold text-white">Bot Command Simulator (@Anime4uB2Bot)</h2>
          <p className="text-xs text-slate-400 mt-1">
            Tests python-telegram-bot command handlers, inline keyboards, duplicate modals, and channel posting.
          </p>
        </div>

        <div className="flex items-center space-x-2 text-xs bg-slate-950 px-3 py-2 rounded-xl border border-slate-800">
          <ShieldCheck className="w-4 h-4 text-emerald-400" />
          <span className="text-slate-300 font-mono">Admin Authorization Checked</span>
        </div>
      </div>

      {/* Chat Window */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl flex flex-col h-[520px] overflow-hidden shadow-2xl">
        {/* Chat Body */}
        <div className="flex-1 p-4 overflow-y-auto space-y-4 bg-slate-950/60">
          {messages.map((m) => (
            <div
              key={m.id}
              className={`flex flex-col ${m.sender === 'user' ? 'items-end' : 'items-start'}`}
            >
              <div
                className={`max-w-md rounded-2xl p-4 text-xs font-sans leading-relaxed space-y-2 ${
                  m.sender === 'user'
                    ? 'bg-indigo-600 text-white rounded-br-none shadow-md shadow-indigo-600/20'
                    : 'bg-slate-900 border border-slate-800 text-slate-200 rounded-bl-none shadow-md'
                }`}
              >
                <div className="whitespace-pre-wrap font-sans">{m.text}</div>

                {/* Inline Keyboard Buttons */}
                {m.buttons && (
                  <div className="pt-2 border-t border-slate-800/80 space-y-1.5">
                    {m.buttons.map((row, rIdx) => (
                      <div key={rIdx} className="flex gap-1.5">
                        {row.map((btn, bIdx) => (
                          <button
                            key={bIdx}
                            onClick={() => handleButtonClick(btn.action, btn.text)}
                            className="flex-1 px-3 py-1.5 rounded-lg bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 text-[11px] font-semibold transition-all text-center"
                          >
                            {btn.text}
                          </button>
                        ))}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <span className="text-[10px] text-slate-500 font-mono mt-1 px-1">{m.timestamp}</span>
            </div>
          ))}
        </div>

        {/* Command Quick Bar */}
        <div className="p-2 bg-slate-900 border-t border-slate-800/80 flex items-center space-x-2 overflow-x-auto text-xs font-mono text-slate-400">
          <span className="text-slate-500 px-2 shrink-0">Quick Commands:</span>
          {['/start', '/folders', '/newfolder', '/selected', '/cancel', '/search'].map((cmd) => (
            <button
              key={cmd}
              onClick={() => processCommand(cmd)}
              className="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-indigo-600 hover:text-white transition-all shrink-0"
            >
              {cmd}
            </button>
          ))}
        </div>

        {/* Chat Input Bar */}
        <form onSubmit={handleSend} className="p-3 bg-slate-900 border-t border-slate-800 flex items-center space-x-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Type command (/start, /folders, /newfolder) or file name..."
            className="flex-1 px-4 py-2.5 bg-slate-950 border border-slate-800 rounded-xl text-slate-200 text-xs font-mono focus:outline-none focus:border-indigo-500"
          />
          <button
            type="submit"
            className="p-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white shadow-md shadow-indigo-600/30 transition-all shrink-0"
          >
            <Send className="w-4 h-4" />
          </button>
        </form>
      </div>
    </div>
  );
};
