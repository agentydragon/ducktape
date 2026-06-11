/**
 * Puppeteer configuration for Bazel builds.
 *
 * Skip browser downloads during lifecycle hooks - browsers are only needed
 * for running tests, not for building. This is especially important in
 * sandboxed environments (like gVisor) where DNS may not work.
 *
 * @type {import("puppeteer").Configuration}
 */
module.exports = {
  chrome: {
    skipDownload: true,
  },
  "chrome-headless-shell": {
    skipDownload: true,
  },
  firefox: {
    skipDownload: true,
  },
};
