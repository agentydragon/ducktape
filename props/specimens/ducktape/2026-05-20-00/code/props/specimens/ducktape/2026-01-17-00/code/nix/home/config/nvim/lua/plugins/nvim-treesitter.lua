-- disable in vscode
if vim.g.vscode then return {} end

local M = {
	"nvim-treesitter/nvim-treesitter",
	build = ":TSUpdate",
	--build = function()
	--	require("nvim-treesitter.install").update({ with_sync = true })()
	--end,
	event = { "BufReadPost", "BufNewFile" },
	config = function()
		-- Setup folding using treesitter
		-- (Start with all folds open, but allow closing them)
		vim.opt.foldmethod = "expr"
		vim.opt.foldexpr = "v:lua.vim.treesitter.foldexpr()"
		vim.opt.foldlevelstart = 99
		vim.opt.foldlevel = 99

		-- nvim-treesitter 1.0+ removed require("nvim-treesitter.configs")
		-- Now uses require("nvim-treesitter").setup() directly
		require("nvim-treesitter").setup({
			ensure_installed = {
				"bash",
				"bibtex",
				"c",
				"c_sharp",
				"clojure",
				"cmake",
				"cpp",
				"css",
				"csv",
				"desktop",
				"diff",
				"dockerfile",
				"git_config",
				"git_rebase",
				"gitattributes",
				"gitcommit",
				"gitignore",
				"go",
				"gomod",
				"gosum",
				"gotmpl",
				"haskell",
				"html",
				"htmldjango",
				"http",
				"ini",
				"java",
				"javadoc",
				"javascript",
				"jinja",
				"jq",
				"jsdoc",
				"json",
				"jsonnet",
				"latex",
				"lua",
				"luadoc",
				"make",
				"markdown",
				"nginx",
				"nix",
				"proto",
				"python",
				"requirements",
				"rust",
				"scss",
				"sql",
				"ssh_config",
				"starlark",
				"textproto",
				"tmux",
				"toml",
				"typescript",
				"vim",
				"vimdoc",
				"xml",
			},
			auto_install = true,
		})

		-- Highlighting and indentation are now enabled by default in Neovim 0.10+
		-- via vim.treesitter.start() which is called automatically
	end,
}
return { M }
