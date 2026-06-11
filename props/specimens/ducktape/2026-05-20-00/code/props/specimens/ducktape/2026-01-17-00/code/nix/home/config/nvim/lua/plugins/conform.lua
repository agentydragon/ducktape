-- disable in vscode - vscode has its own formatting system
if vim.g.vscode then return {} end

return {
	"stevearc/conform.nvim",
	opts = {

		formatters_by_ft = {
			lua = { "stylua" },
			python = { "isort", "black" }, -- multiple formatters sequentially
			-- You can customize some of the format options for the filetype (:help conform.format)
			rust = { "rustfmt", lsp_format = "fallback" },
			-- Conform will run the first available formatter
			--javascript = { "prettierd", "prettier", stop_after_first = true },
		},
        format_on_save = {
            -- These options will be passed to conform.format()
            timeout_ms = 500,
            lsp_format = "fallback",
        },
	},
}
