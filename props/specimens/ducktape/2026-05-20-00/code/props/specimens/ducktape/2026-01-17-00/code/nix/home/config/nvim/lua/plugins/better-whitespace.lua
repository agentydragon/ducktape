-- disable in vscode - vscode has built-in whitespace highlighting/trimming
if vim.g.vscode then return {} end

return {
	"ntpeters/vim-better-whitespace",
	event = { "BufReadPre", "BufNewFile" },
	init = function()
		vim.g.better_whitespace_enabled = 1
		-- Stripping whitespace on save is by default disabled.

		-- Highlight single-line mixed indentation (tabs+spaces)
		vim.api.nvim_set_hl(0, "ExtraIndentMixed", { bg = "#443333" })
		vim.api.nvim_create_autocmd("BufWinEnter", {
			callback = function()
				vim.fn.matchadd("ExtraIndentMixed", [[^\t+ +\|^ \+\t+]])
			end,
		})

		-- Highlight trailing whitespace (still from plugin)
		vim.api.nvim_set_hl(0, "ExtraWhitespace", { bg = "#552222" })
	end,
}
