# Claude Code session environment (auto-generated)
% for path in prepend_paths:
[ -d ${path | sh} ] && export PATH=${path | sh}:"$PATH"
% endfor
% for key, value in exports.items():
export ${key}=${value | sh}
% endfor
