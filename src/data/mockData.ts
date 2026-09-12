import { StreamItem, StorageFolder, AppConfig } from '../types';

export const initialConfig: AppConfig = {
  botToken: '8769661029:AAED5_SSFoU-Q_xQ_-p-x5FqzU7J9MZcIaE',
  adminIds: '123456789,987654321',
  storageChannelId: '-1001234567890',
  maxFileSize: 2147483648, // 2 GB
  autoHls: false,
  tempDir: './downloads',
  supabaseUrl: 'https://xyzcompany.supabase.co',
  supabaseRoleKey: 'eyJhYnExMjM0NTYiOiJzZXJ2aWNlX3JvbGUiLCJpYXQiOjE2ODAwMDAwMDB9...',
};

export const initialFolders: StorageFolder[] = [
  { name: 'anime/solo-leveling/season-1', path: 'anime/solo-leveling/season-1', fileCount: 12, updatedAt: '2026-09-12' },
  { name: 'anime/demon-slayer/hashira-arc', path: 'anime/demon-slayer/hashira-arc', fileCount: 8, updatedAt: '2026-09-11' },
  { name: 'anime/jujutsu-kaisen/shibuya-incident', path: 'anime/jujutsu-kaisen/shibuya-incident', fileCount: 18, updatedAt: '2026-09-08' },
  { name: 'movies/action/2026', path: 'movies/action/2026', fileCount: 5, updatedAt: '2026-09-10' },
  { name: 'series/chainsaw-man/season-1', path: 'series/chainsaw-man/season-1', fileCount: 12, updatedAt: '2026-09-01' },
];

export const initialStreams: StreamItem[] = [
  {
    id: 'f81d4fae-7dec-11d0-a765-00a0c91e6bf6',
    title: 'Solo Leveling S1 Ep 12 - Arise',
    video_url: 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4',
    storage_message_id: 1001,
    storage_channel_id: -1001234567890,
    file_unique_id: 'AQAD_A6x123abc',
    file_name: 'solo_leveling_e12.mkv',
    is_live: true,
    created_at: '2026-09-12 11:30:00',
    folder_path: 'anime/solo-leveling/season-1',
    file_size: '348.2 MB',
    mime_type: 'video/x-matroska',
    media_type: 'video',
  },
  {
    id: 'e28b1234-9abc-4567-def0-123456789abc',
    title: 'Demon Slayer Hashira Training Ep 08',
    video_url: 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4',
    storage_message_id: 1002,
    storage_channel_id: -1001234567890,
    file_unique_id: 'AQAD_B7y456def',
    file_name: 'demon_slayer_e08.mkv',
    is_live: true,
    created_at: '2026-09-11 18:45:12',
    folder_path: 'anime/demon-slayer/hashira-arc',
    file_size: '512.4 MB',
    mime_type: 'video/x-matroska',
    media_type: 'video',
  },
  {
    id: 'c73a9876-5432-10fe-dcba-9876543210fe',
    title: 'Jujutsu Kaisen S2 Ep 17 - Thunderclap',
    video_url: 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4',
    storage_message_id: 1003,
    storage_channel_id: -1001234567890,
    file_unique_id: 'AQAD_C8z789ghi',
    file_name: 'jjk_e17.mp4',
    is_live: false,
    created_at: '2026-09-09 14:20:00',
    folder_path: 'anime/jujutsu-kaisen/shibuya-incident',
    file_size: '620.1 MB',
    mime_type: 'video/mp4',
    media_type: 'video',
  },
];
