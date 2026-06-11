-- disable in vscode
if vim.g.vscode then return {} end

-- 6. **LSP & Syntax (Treesitter)** - (Optional modern additions)
return {
	"neovim/nvim-lspconfig",
	event = "BufReadPre",
	config = function()
		-- Example: enable Pyright LSP for Python (add other language servers as needed)
		vim.lsp.config("pyright", {})
		vim.lsp.enable("pyright")
	end,
}
