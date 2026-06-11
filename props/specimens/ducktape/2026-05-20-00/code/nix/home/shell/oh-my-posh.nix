# oh-my-posh prompt configuration
# Not enabled by default — requires USE_OHMYPOSH env var.
{ pkgs, ... }:
let
  toJSON = (pkgs.formats.json { }).generate;

  powerlineRight = "\ue0b2";
  powerlineLeft = "\ue0b0";

  segment =
    {
      type,
      style ? "powerline",
      foreground,
      background,
      template,
      properties ? { },
      background_templates ? [ ],
      powerline_symbol ? null,
      invert_powerline ? false,
    }:
    {
      inherit
        type
        style
        foreground
        background
        template
        ;
    }
    // (if powerline_symbol != null then { inherit powerline_symbol; } else { })
    // (if invert_powerline then { inherit invert_powerline; } else { })
    // (if properties != { } then { inherit properties; } else { })
    // (if background_templates != [ ] then { inherit background_templates; } else { });
in
{
  xdg.configFile."oh-my-posh/config.json".source = toJSON "oh-my-posh-config.json" {
    "$schema" = "https://raw.githubusercontent.com/JanDeDobbeleer/oh-my-posh/main/themes/schema.json";
    version = 2;
    final_space = true;
    console_title_template = "{{ .Shell }} in {{ .Folder }}";

    blocks = [
      {
        type = "prompt";
        alignment = "left";
        segments = [
          (segment {
            type = "session";
            powerline_symbol = powerlineLeft;
            foreground = "yellow";
            background = "black";
            template = "{{ if .SSHSession }}{{ .UserName }}@{{ .HostName }} {{ end }}";
          })
          (segment {
            type = "path";
            powerline_symbol = powerlineLeft;
            foreground = "white";
            background = "blue";
            properties = {
              style = "agnoster_short";
              max_depth = 3;
            };
            template = " {{ .Path }} ";
          })
          (segment {
            type = "git";
            powerline_symbol = powerlineLeft;
            foreground = "black";
            background = "green";
            background_templates = [
              "{{ if or (.Working.Changed) (.Staging.Changed) }}yellow{{ end }}"
              "{{ if and (gt .Ahead 0) (gt .Behind 0) }}red{{ end }}"
              "{{ if gt .Ahead 0 }}cyan{{ end }}"
              "{{ if gt .Behind 0 }}magenta{{ end }}"
            ];
            properties = {
              fetch_status = true;
              fetch_stash_count = true;
              branch_max_length = 32;
            };
            template = " {{ .HEAD }}{{ if .BranchStatus }} {{ .BranchStatus }}{{ end }}{{ if .Working.Changed }} !{{ .Working.Changed }}{{ end }}{{ if .Staging.Changed }} +{{ .Staging.Changed }}{{ end }}{{ if gt .StashCount 0 }} *{{ .StashCount }}{{ end }} ";
          })
        ];
      }
      {
        type = "rprompt";
        segments = [
          (segment {
            type = "status";
            powerline_symbol = powerlineRight;
            invert_powerline = true;
            foreground = "yellow";
            background = "red";
            template = " ✘ {{ .Code }} ";
          })
          (segment {
            type = "sudo";
            powerline_symbol = powerlineRight;
            invert_powerline = true;
            foreground = "yellow";
            background = "darkGray";
            template = " ⚡ ";
          })
          (segment {
            type = "executiontime";
            powerline_symbol = powerlineRight;
            invert_powerline = true;
            foreground = "black";
            background = "yellow";
            properties = {
              threshold = 3000;
              style = "austin";
            };
            template = " {{ .FormattedMs }} ";
          })
          (segment {
            type = "nix-shell";
            powerline_symbol = powerlineRight;
            invert_powerline = true;
            foreground = "black";
            background = "blue";
            template = "  {{ .Type }} ";
          })
          (segment {
            type = "time";
            powerline_symbol = powerlineRight;
            invert_powerline = true;
            foreground = "black";
            background = "white";
            properties.time_format = "15:04:05";
            template = " {{ .CurrentDate | date .Format }} ";
          })
        ];
      }
    ];

    transient_prompt.template = "> ";
  };
}
