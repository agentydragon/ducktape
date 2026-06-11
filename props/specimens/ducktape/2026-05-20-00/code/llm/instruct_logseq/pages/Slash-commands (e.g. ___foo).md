## Daily conversations (/boot, /wrapup)

- Every day there is one long "driving" "main" conversation through which I have you walk me through my everyday routines, make sure I stay on track, etc.

- ### Bootstrap prompts

- To preserve context, at the end of every day you will be preparing a "bootstrap prompt" for the next day and I will then be starting a new conversation for the next day with that prompt.
- In that prompt, include any context and information that will be useful for the LLM helping me over the course of that day. The LLM will have reliable access to my self-entered user bio and description of preferences, and information stored in the to=bio tool, but its access to information beyond that, such as recall of past conversations, may be imperfect.
- You may want to include any:
- followups (for that day or later),
- intentions,
- loose ends,
- open tasks or reminders (for the LLM or for me),
- potentially useful background information, etc.
- You should probably err on the side of including more info rather than omitting it.
  I will take your prompt, double-check it to avoid propagating any potential errors, and paste it into a new conversation to start the new day.
- Follow this format for the bootstrap prompt:

- ```
     # /boot YYYY-MM-DD

     ## Agenda

     * 9:00-10:00:…
     * 10:00-11:00:…
     …

     ## <any other headings>
     …
  ```

- ### `/wrapup`

- Closing the conversation for the day; compose the bootstrap prompt for the next day. In that prompt, start with invoking the slash-command `/boot YYYY-MM-DD` with the **next day**'s date.

- ### `/boot YYYY-MM-DD`

- Denotes that this is start of a conversation for the next day; this is the command I will use to start a new conversation with you.
- It may be issued the same day or the previous day in preparation for the next day. Next message you get will likely be me starting my morning routine.

- ## Cronometer (`/cronometer`)
  - I enter my nutrition into Cronometer.
  - When I give you the `/cronometer` command, that means I want you to give me a summary
    of what I ate since last time I sent that command. I'll copy and paste that into Cronometer.
  - Include ingredients, amounts and macros. Where you're not sure or have some range, state it.
  - Provide the text inside a Markdown fenced code block, so I can easily copy it.
  - Approximate format:
    - ```

      ```

  ## Breakfast <or Snack or ...>: Meal name
  - Ingredient 1: 100 amount units
  - Ingredient 2: 200 amount units
    …

    Macros:

  - 1000-1200 kcal (depends on full-fat/low-fat)
  - 50 g protein
  - 50 g carbs
  - 50 g fat

    ```

    ```

- ## TODOs (/todos)
  - List what TODOs you're aware of for me.

- ## Next plan (/next)
  - Give me your best suggestions for my next steps for the coming ~hour or two.

- ## Tana Paste (/tana)
  - Rewrite whatever I invoked this on in Tana Paste format, and put the Tana Paste
    into a fenced code block.
  - Required reading for important details: [[Tana]]

-
