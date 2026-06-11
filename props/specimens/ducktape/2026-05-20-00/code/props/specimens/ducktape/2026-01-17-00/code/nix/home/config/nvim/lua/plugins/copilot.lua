-- disable in vscode
if vim.g.vscode then return {} end

return {
  {
    "zbirenbaum/copilot.lua",
    event = "InsertEnter",
    cmd = "Copilot",   -- lets you run :Copilot <…> commands
    opts = {
      -- turn on inline suggestions immediately when typing
      suggestion = { enabled = true, auto_trigger = true },
      panel = { enabled = false }, -- keep the side panel off unless you want it
      -- optional: restrict/allow by filetype
      filetypes = {
        markdown = true,
        help = true,
        gitcommit = true,
        ["*"] = true
      },
      -- optional: use binary server if you don't want to install Node:
      -- server = { type = "binary" },
      --
      -- :Copilot auth   " opens browser; sign in
      -- :Copilot status " should show "Ready"
    },
  },
}
