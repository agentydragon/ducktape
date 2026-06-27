type HakuAssetGlobals = {
  __HAKU_ASSETS__?: {
    logo?: string;
  };
};

const assets = (globalThis as HakuAssetGlobals).__HAKU_ASSETS__;

export const LOGO_URL = assets?.logo ?? "/logo.svg";
