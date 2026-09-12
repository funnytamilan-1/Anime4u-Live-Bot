export interface StreamItem {
  id: string;
  title: string;
  video_url: string;
  storage_message_id: number;
  storage_channel_id: number;
  file_unique_id: string;
  file_name: string;
  is_live: boolean;
  created_at: string;
  folder_path: string;
  file_size?: string;
  mime_type?: string;
  media_type?: string;
}

export interface StorageFolder {
  name: string;
  path: string;
  fileCount: number;
  updatedAt: string;
}

export interface UploadJob {
  id: string;
  fileName: string;
  fileSize: number;
  folderPath: string;
  status: 'queued' | 'downloading' | 'storing_telegram' | 'saving_metadata' | 'completed' | 'failed';
  progress: number;
  log: string;
  storageMessageId?: number;
  storageChannelId?: number;
  fileUniqueId?: string;
  isDuplicate?: boolean;
  duplicateInfo?: StreamItem;
  addedToDatabase: boolean;
  error?: string;
}

export interface BotMessage {
  id: string;
  sender: 'user' | 'bot';
  text: string;
  timestamp: string;
  buttons?: { text: string; action: string }[][];
}

export interface AppConfig {
  botToken: string;
  adminIds: string;
  storageChannelId: string;
  maxFileSize: number;
  autoHls: boolean;
  tempDir: string;
  supabaseUrl: string;
  supabaseRoleKey: string;
}
