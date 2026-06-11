-- disable in vscode
if vim.g.vscode then return {} end

-- Previously on Mac: maxmx03 had screaming diff colors
-- But trying again as it's more actively maintained and feature-rich

-- Current: maxmx03/solarized.nvim (more features, variants)
return {
	"maxmx03/solarized.nvim",
	lazy = false,
	priority = 1000,
	---@type solarized.config
	opts = {},
	config = function(_, opts)
		vim.o.termguicolors = true
		require("solarized").setup(opts)
		vim.cmd.colorscheme("solarized")
	end,
}

--[[ Alternative: Tsuzat/NeoSolarized.nvim (colors were fine)
-- Switch to this if maxmx03 has diff color issues again
return {
	"Tsuzat/NeoSolarized.nvim",
	lazy = false,
	priority = 1000,
	config = function()
		vim.cmd("colorscheme NeoSolarized")
	end,
}
--]]
