export type WorldBenchmark = {
  id: string;
  name: string;
  author: string;
  thumbnail: string;
  visits: number;
  favorites: number;
  samples: number;
  sessions: number;
  score: number | null;
  gpuMs: number | null;
  cpuMs: number | null;
  populationMin: number | null;
  populationMax: number | null;
  scale: number | null;
  mode: string;
  transitionRecords?: number;
};

export const snapshotAt = "2026-07-26 12:20 CST";

export const worlds: WorldBenchmark[] = [
  {
    id: "wrld_08a0a55d-3a59-4d19-8f2a-f0c5cc05f684",
    name: "ちょっと気まずい三人専用部屋",
    author: "工場長 じろう",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_0b4ba1a6-6ede-4ade-b93b-da296968f9ca/3/256",
    visits: 248076, favorites: 10599, samples: 599, sessions: 1, score: 79,
    gpuMs: 17.1, cpuMs: 4.4, populationMin: 1, populationMax: 3,
    scale: 100, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_b4eef105-5db1-4c1f-8800-a6e6de4a20e7",
    name: "こういう部屋に住みたい",
    author: "ParusuRblx",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_6f61b4e7-671d-466b-b24f-33ea377b5a6c/1/256",
    visits: 919340, favorites: 18954, samples: 510, sessions: 2, score: 48,
    gpuMs: 12.7, cpuMs: 3.1, populationMin: 2, populationMax: 2,
    scale: 20, mode: "72Hz / 72FPS",
  },
  {
    id: "wrld_8be3a78b-5f04-468e-a414-ddfafa011e3f",
    name: "星見寝 - Starlit Slumber ⁄ おやすみ星空空間 -",
    author: "Kashiwam",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_4273d469-bd03-4cff-b89d-474f2a7994d7/5/256",
    visits: 216254, favorites: 5816, samples: 411, sessions: 5, score: 95,
    gpuMs: 21.5, cpuMs: 4.1, populationMin: 1, populationMax: 2,
    scale: 100, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_4d353dc5-a983-4f11-96f8-e6752d627eb3",
    name: "群青星海",
    author: "！すらいむ！",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_ac42822c-84b7-4315-a5c6-9539b2129ec5/4/256",
    visits: 436142, favorites: 13210, samples: 302, sessions: 1, score: 74,
    gpuMs: 18.9, cpuMs: 9.3, populationMin: 2, populationMax: 17,
    scale: 100, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_96203704-8a95-4cb2-a047-794b6b029807",
    name: "N o n e ․",
    author: "YoonHa",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_7875f398-c9ed-46b4-8c7e-00b2f5325360/12/256",
    visits: 461029, favorites: 10575, samples: 295, sessions: 1, score: 97,
    gpuMs: 21.2, cpuMs: 4.1, populationMin: 1, populationMax: 4,
    scale: 87, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_aeba3422-1543-4e6f-bd9d-0f41ddc5c4f8",
    name: "VR Poker",
    author: "Nex1942",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_f48bcb8e-e85a-4e4f-a1ce-3868c0311b8c/2/256",
    visits: 2235998, favorites: 56760, samples: 51, sessions: 4, score: 73,
    gpuMs: 19.7, cpuMs: 18.1, populationMin: 8, populationMax: 20,
    scale: 100, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_d70113be-bdb7-43dc-bf8e-8c8510a13738",
    name: "BarParenchan-V4․1",
    author: "Edge-One",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_583de779-ff6a-4c50-a068-b71c2bc06731/1/256",
    visits: 8490, favorites: 0, samples: 46, sessions: 1, score: 93,
    gpuMs: 13.8, cpuMs: 15.7, populationMin: 26, populationMax: 31,
    scale: 130, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_75c9e66c-36d1-4c32-b785-d413bf5e1062",
    name: "优势在我",
    author: "-帕休-",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_cf78fc2f-9c00-4fdd-87c2-9b612b43a8ed/1/256",
    visits: 100, favorites: 9, samples: 14, sessions: 1, score: 100,
    gpuMs: 13.4, cpuMs: 15.9, populationMin: 3, populationMax: 4,
    scale: 100, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_394304f6-9938-40df-ab98-35ba57e3f75b",
    name: "夜",
    author: "Miro-cn",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_1c76e781-faef-429e-b150-a02c24eb544f/4/256",
    visits: 1086412, favorites: 12929, samples: 9, sessions: 1, score: 67,
    gpuMs: 16.6, cpuMs: 22.4, populationMin: 10, populationMax: 16,
    scale: 100, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_f18916ae-e83d-4255-be4a-953b31dc6317",
    name: "2人だけのASMR room",
    author: "ponchof",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_afc4dc9e-2769-43c5-b79c-dbea6a6b4c67/2/256",
    visits: 90047, favorites: 6113, samples: 8, sessions: 1, score: 25,
    gpuMs: 12, cpuMs: 8.6, populationMin: 2, populationMax: 2,
    scale: 50, mode: "72Hz / 72FPS",
  },
  {
    id: "wrld_f8719834-9487-4835-bc2d-7b6e79902c78",
    name: "ロリＶ睡部 ～おやすみ VR SleepClub～",
    author: "Fe_falsename",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_cb1f08f4-cd26-4d97-8067-ac090366a1af/4/256",
    visits: 26646, favorites: 387, samples: 8, sessions: 2, score: 50,
    gpuMs: 15.2, cpuMs: 15.4, populationMin: 2, populationMax: 4,
    scale: 72, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_ff5f8330-3bab-49ac-8c41-10175a929295",
    name: "すやるーむ - SuyaRoom",
    author: "猫宮くるみ",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_2947d88b-7d46-4d1c-9732-81668b5e7beb/13/256",
    visits: 487466, favorites: 7734, samples: 6, sessions: 1, score: 33,
    gpuMs: 17.6, cpuMs: 8.3, populationMin: 4, populationMax: 4,
    scale: 58, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_93cb25d9-c3c5-4670-b0fa-c83a730f2c54",
    name: "Just A Cabin",
    author: "Maru~",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_68ab4d61-d153-494d-ba32-da8052ff469e/2/256",
    visits: 1061847, favorites: 23066, samples: 3, sessions: 1, score: 67,
    gpuMs: 21.9, cpuMs: 12.5, populationMin: 2, populationMax: 2,
    scale: 82, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_31554a12-33a0-4433-be8f-4b7bb41afbbe",
    name: "Change the weather",
    author: "mo_末",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_693ab760-7c4f-44a2-b3e3-6f70e68fd8c7/1/256",
    visits: 60536, favorites: 3576, samples: 1, sessions: 1, score: 0,
    gpuMs: 23.6, cpuMs: 21, populationMin: 5, populationMax: 5,
    scale: 60, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_914d05cd-b430-4e1d-bef6-c30b56bfd4f8",
    name: "Vr Poker Beta Test",
    author: "杰哥jay",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_a7c6befc-b30a-4da1-9a79-3e00de9561e6/3/256",
    visits: 996, favorites: 7, samples: 1, sessions: 1, score: 100,
    gpuMs: 18.8, cpuMs: 20.1, populationMin: 4, populationMax: 4,
    scale: 100, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_fb784b28-8a50-4487-9bc5-913b23e6d3fa",
    name: "挂机修仙․ver1․3beta",
    author: "EthernetNeko",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_84839809-6413-4203-be65-405d2acb47e3/2/256",
    visits: 85635, favorites: 1343, samples: 1, sessions: 1, score: 100,
    gpuMs: 15.1, cpuMs: 14.8, populationMin: 3, populationMax: 3,
    scale: 100, mode: "72Hz / 36FPS",
  },
  {
    id: "wrld_7345f139-e021-45cb-986e-b8b0f43994fa",
    name: "VRC-JP 初心者プラザ ［JP］",
    author: "V WORLD",
    thumbnail: "https://api.vrchat.cloud/api/1/image/file_f9916bf3-9e17-48f5-bd5d-d224f8e65c45/14/256",
    visits: 1287858, favorites: 36305, samples: 0, sessions: 0, score: null,
    gpuMs: null, cpuMs: null, populationMin: null, populationMax: null,
    scale: null, mode: "无稳定窗口", transitionRecords: 5,
  },
];
