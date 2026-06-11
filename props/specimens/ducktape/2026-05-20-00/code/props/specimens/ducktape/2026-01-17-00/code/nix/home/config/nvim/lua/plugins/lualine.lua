-- disable in vscode
if vim.g.vscode then return {} end

-- status line, like airline
return {
	"nvim-lualine/lualine.nvim",
	dependencies = { "nvim-tree/nvim-web-devicons" },
	opts = {
		icons_enabled = true,
		theme = "auto",
	},
}
