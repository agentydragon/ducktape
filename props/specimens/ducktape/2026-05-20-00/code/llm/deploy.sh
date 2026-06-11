#!/bin/bash
rsync -avh --delete --progress \
  ./instruct-logseq-export/* \
  root@agentydragon.com:/var/www/agentydragon.com/llm-instruct
