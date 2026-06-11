-- Restores last edited position in buffer upon load.
-- Keep enabled in vscode - cursor position restoration is useful there too
return {
	"farmergreg/vim-lastplace",
	lazy = false, -- load immediately on startup
	init = function() end,
}
