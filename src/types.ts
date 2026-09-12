export interface StreamItem {
  id: string;
  title: string;
  video_url: string;
  storage_message_id?: number;
  storage_channel_id?: number;
  b2_file_id?: string;
  b2_path?: string;
  b2_url?: string;
  storage_mode: 'b2' | 'telegram' | 'both';
  b2_status: 'success' | 'failed' | 'none';
  telegram_status: 'success' | 'failed' | 'none';
  status: 'complete' | 'partial_success' | 'failed';
  file_unique_id?: string;
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
  storageMode?: 'b2' | 'telegram' | 'both' | null;
  fileCount: number;
  updatedAt: string;
}

export interface UploadJob {
  id: string;
  fileName: string;
  fileSize: number;
  folderPath: string;
  storageMode: 'b2' | 'telegram' | 'both';
  status: 'queued' | 'downloading' | 'storing_b2' | 'storing_telegram' | 'saving_metadata' | 'completed' | 'partial_success' | 'failed';
  progress: number;
  log: string;
  b2Path?: string;
  b2Url?: string;
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
  storageMode: 'b2' | 'telegram' | 'both';
  storageChannelId: string;
  b2KeyId: string;
  b2Key: string;
  b2Bucket: string;
  b2PublicBaseUrl: string;
  maxFileSize: number;
  autoHls: boolean;
  tempDir: string;
  supabaseUrl: string;
  supabaseRoleKey: string;
}
