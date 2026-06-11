--[[
set tabstop=8
set shiftwidth=8
set softtabstop=8

-- fold by treesitter, open all by default
--set foldmethod=syntax
--set foldlevelstart=20

set omnifunc=syntaxcomplete#Complete

" no mistyped :w or :q...
:command W w
:command Q q

set laststatus=2

Not yet ported plugins I had listed in old vim config:
  wincent/command-t, tpope/vim-fugitive, pbrisbin/vim-mkdir' " mkdir needed dirs before writing buffer
  bazelbuild/vim-ft-bzl, google/vim-maktaba (vim-codefmt dep), vim-codefmt, vim-glaive (configures codefmt's maktaba flags),
  leafgarland/typescript-vim, wellle/context.vim

 set textwidth=80

autocmd Filetype c setlocal cindent nosmartindent
" TODO: enable this settings
" autocmd Filetype cpp setlocal cindent nosmartindent tabstop=2 softtabstop=2 expandtab

let g:netrw_banner=0  " Hide netrw banner

let g:matchparen_insert_timeout=5

set colorcolumn=80,+0  " Highlight column 80

-- Set codefmt autoformatter settings
augroup autoformat_settings
  autocmd FileType bzl AutoFormatBuffer buildifier
  autocmd FileType c,cpp,proto,javascript AutoFormatBuffer clang-format
  autocmd FileType go AutoFormatBuffer gofmt
  autocmd FileType rust AutoFormatBuffer rustfmt
  " autocmd FileType html,css,json AutoFormatBuffer js-beautify
  autocmd FileType java AutoFormatBuffer google-java-format
augroup END

--set foldmethod=syntax
set foldcolumn=1
--let javaScript_fold=1 "activate folding by JS syntax
set foldlevelstart=99

" Map Alt-T to paste current datetime
inoremap <A-t> <C-R>=strftime('%Y-%m-%d %H:%M:%S')<C-M>
]]
--

-- Hide useless stuff in netrw
vim.api.nvim_create_autocmd("FileType", {
	pattern = "netrw",
	callback = function()
		vim.g.netrw_list_hide = table.concat({
			vim.fn["netrw_gitignore#Hide"](), -- auto hide based on .gitignore
			".*%.sw[op]$", -- Vim swap
			"^\\./$", -- current dir entry
			-- '^\\.\\./$', -- parent dir entry (TODO: use - key)
			".*/%.git/$", -- .git dirs
		}, ",")
	end,
})

-- TODO: give warning or highlight if the same file has a mix of lines starting with spaces and tabs
-- (rather that just one of the two options)

-- u mad bro?
local i = vim.keymap.set
for lhs, rhs in pairs({
	["<Left>"] = "←",
	["<Right>"] = "→",
	["<Up>"] = "↑",
	["<Down>"] = "↓",
	["<S-Left>"] = "⇐",
	["<S-Right>"] = "⇒",
	["<S-Up>"] = "⇑",
	["<S-Down>"] = "⇓",
}) do
	i("i", lhs, rhs)
end

local opt = vim.opt
opt.scrolloff = 5 -- at least 5 lines between cursor and end of page
opt.number = true -- Show line numbers
opt.hlsearch = true -- Highlight search matches
opt.incsearch = true -- Show search matches as you type
opt.wrap = false -- Disable line wrap
opt.expandtab = true -- Use spaces instead of tabs
opt.ignorecase = true -- Case-insensitive search...
opt.smartcase = true -- ... but case-sensitive if uppercase char present
opt.undofile = true -- Enable persistent undo
opt.updatetime = 1000 -- write swap file if nothing typed for 1000 ms (default is 4000ms)

opt.shiftwidth = 4 -- Indent size (width of an indent)
opt.tabstop = 4 -- Number of spaces tabs count for
opt.smartindent = true -- Smart indenting on new lines
-- opt.cursorline = true           -- Highlight the current line
opt.termguicolors = true -- Enable true color support (recommended for modern UI)

-- (not mine, need to check:)
-- opt.swapfile = false            -- Don't use swapfile
-- opt.backup = false              -- Don't create backup files

-- maybe -- try out:
-- opt.relativenumber = true       -- Show relative line numbers outside current line
-- opt.splitright = true           -- Split vertical windows to the right
-- opt.splitbelow = true           -- Split horizontal windows to the bottom

-- Make sure to setup `mapleader` and `maplocalleader` before
-- loading lazy.nvim so that mappings are correct.
-- This is also a good place to setup other settings (vim.opt)
vim.g.mapleader = " "
vim.g.maplocalleader = "\\"

-- Enable EditorConfig support
vim.g.editorconfig = true

require("config.lazy")

-- Django template formatter:
-- { "yaegassy/coc-htmldjango" }
