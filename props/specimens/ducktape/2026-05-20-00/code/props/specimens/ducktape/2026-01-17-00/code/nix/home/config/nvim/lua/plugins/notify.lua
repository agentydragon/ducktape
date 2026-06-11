-- disable in vscode
if vim.g.vscode then return {} end

-- Enhanced notification system
return {
	"rcarriga/nvim-notify",
	lazy = false,
	priority = 100,
	config = function()
		-- Use solarized background colors
		local bg_color = vim.o.background == "dark" and "#002b36" or "#fdf6e3"
		require("notify").setup({
			background_colour = bg_color,
		})
		vim.notify = require('notify')
	end,
}
