# Shared neovim configuration
{
  pkgs,
  ...
}:
let
  gnomeNvim = pkgs.vimUtils.buildVimPlugin {
    pname = "gnome.nvim";
    version = "2024-11-26";
    src = pkgs.fetchFromGitHub {
      owner = "willmcpherson2";
      repo = "gnome.nvim";
      rev = "87e850c1e9422310ede4b70df90a6a89c16bb9e1";
      sha256 = "1zxq484k3mcppy21xiflmnji7j2n5zyc74ffbybhc9xasrgwa1nk";
    };
  };

  vimLumen = pkgs.vimUtils.buildVimPlugin {
    pname = "vim-lumen";
    version = "2024-11-26";
    src = pkgs.fetchFromGitHub {
      owner = "vimpostor";
      repo = "vim-lumen";
      rev = "97157aac9f0d24c144a3defdfe5057ee61e18dcb";
      sha256 = "1a32szs5hz9l1b1s1cfzbjvrn9wzqjkhffq9kaabvbpvlzd2hms9";
    };
  };

  solarizedNvim = pkgs.vimUtils.buildVimPlugin {
    pname = "solarized.nvim";
    version = "2026-04-17";
    src = pkgs.fetchFromGitHub {
      owner = "maxmx03";
      repo = "solarized.nvim";
      rev = "a8085e29883ddcfb39bd46197eb32ef00df05368";
      sha256 = "0psgwfnd5fi0p60pknzz9li70ryxqqjz7gxxvqr0q3q1kzpdhr7q";
    };
  };
in
{
  programs.neovim = {
    enable = true;
    viAlias = true;
    vimAlias = true;
    withNodeJs = false;
    withPython3 = false;
    extraLuaConfig = builtins.readFile ../config/nvim/init.lua;
    plugins = with pkgs.vimPlugins; [
      (nvim-treesitter.withPlugins (
        p: with p; [
          bash
          bibtex
          c
          c-sharp
          clojure
          cmake
          cpp
          css
          csv
          desktop
          diff
          dockerfile
          git-config
          git-rebase
          gitattributes
          gitcommit
          gitignore
          go
          gomod
          gosum
          gotmpl
          haskell
          html
          htmldjango
          http
          ini
          java
          javadoc
          javascript
          jinja
          jq
          jsdoc
          json
          jsonnet
          latex
          lua
          luadoc
          make
          markdown
          nginx
          nix
          proto
          python
          requirements
          rust
          scss
          sql
          ssh-config
          starlark
          textproto
          tmux
          toml
          typescript
          vim
          vimdoc
          xml
        ]
      ))
      {
        plugin = nvim-lspconfig;
        type = "lua";
        config = ''
          vim.lsp.config("pyright", {})
          vim.lsp.enable("pyright")
        '';
      }
      {
        plugin = conform-nvim;
        type = "lua";
        config = ''
          require("conform").setup({
            formatters_by_ft = {
              lua = { "stylua" },
              python = { "isort", "black" },
              rust = { "rustfmt", lsp_format = "fallback" },
            },
            format_on_save = {
              timeout_ms = 500,
              lsp_format = "fallback",
            },
          })
        '';
      }
      {
        plugin = copilot-lua;
        type = "lua";
        config = ''
          require("copilot").setup({
            suggestion = { enabled = true, auto_trigger = true },
            panel = { enabled = false },
            filetypes = {
              markdown = true,
              help = true,
              gitcommit = true,
              ["*"] = true,
            },
          })
        '';
      }
      nvim-web-devicons
      {
        plugin = lualine-nvim;
        type = "lua";
        config = ''
          require("lualine").setup({
            options = { icons_enabled = true, theme = "auto" },
          })
        '';
      }
      {
        plugin = nvim-notify;
        type = "lua";
        config = ''
          local bg_color = vim.o.background == "dark" and "#002b36" or "#fdf6e3"
          require("notify").setup({ background_colour = bg_color })
          vim.notify = require("notify")
        '';
      }
      {
        plugin = vim-better-whitespace;
        type = "lua";
        config = ''
          vim.g.better_whitespace_enabled = 1
          vim.api.nvim_set_hl(0, "ExtraIndentMixed", { bg = "#443333" })
          vim.api.nvim_create_autocmd("BufWinEnter", {
            callback = function()
              vim.fn.matchadd("ExtraIndentMixed", [[^\t+ +\|^ \+\t+]])
            end,
          })
          vim.api.nvim_set_hl(0, "ExtraWhitespace", { bg = "#552222" })
        '';
      }
      vim-lastplace
      {
        plugin = solarizedNvim;
        type = "lua";
        config = ''
          vim.o.termguicolors = true
          require("solarized").setup({})
          vim.cmd.colorscheme("solarized")
        '';
      }
      {
        plugin = gnomeNvim;
        type = "lua";
        config = ''
          if vim.fn.has("unix") == 1 and vim.fn.has("mac") == 0 then
            require("gnome").setup({})
          end
        '';
      }
      vimLumen
    ];
  };
}
