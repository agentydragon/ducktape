-- disable in vscode
if vim.g.vscode then return {} end

-- Platform-specific auto light/dark theme switching
if vim.fn.has("mac") == 1 then
	-- macOS: Use lumen (detects system light/dark mode changes)
	return {
		"vimpostor/vim-lumen",
		lazy = false,
		init = function() end  -- not Lazy-friendly Lua plugin
	}
elseif vim.fn.has("unix") == 1 then
	-- Linux: Use gnome (detects GNOME theme changes via D-Bus)
	return {
		"willmcpherson2/gnome.nvim",
		lazy = false,
		priority = 1100, -- priority > solarized => load before the color scheme
		config = function()
			require("gnome").setup({})
		end,
	}
else
	-- Other platforms: no auto-switching
	return {}
end
