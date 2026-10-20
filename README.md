# sloth-shots 🦥

Your screenshot folder is full of files called `Screenshot 2023-06-17 at 14.11.12.png`. You will never find the one you need.

sloth-shots looks at each screenshot with an open-weight vision model (Gemma 4) and renames it after what's in it. Real output from my own screenshot folder:

```
Screenshot from 2022-11-15 10-01-34.png  ->  2022-11-15_laravel-postgresql-role-does-not-exist.png
Screenshot from 2022-12-14 10-33-31.png  ->  2022-12-14_php-undefined-type-pdf-error.png
Screenshot from 2022-03-29 10-09-20.png  ->  2022-03-29_jsfiddle-javascript-matrix-code-snippet.png
```

Leave `watch` running and you never think about it again. Take a screenshot and about a second later it has a name you can search for in Spotlight.

## Setup

1. Get an API key at <https://openrouter.ai/keys>. A few dollars of credit goes a long way: each screenshot costs about $0.00003.
2. Export it:

   ```sh
   export OPENROUTER_API_KEY=sk-or-...
   ```

3. Run the tool. It has no dependencies beyond Python 3.10+:

   ```sh
   uvx --from . sloth-shots --help
   # or simply
   python3 sloth_shots.py --help
   ```

## Usage

If you don't pass a folder, sloth-shots uses your macOS screenshot location, falling back to `~/Desktop`.

```sh
sloth-shots tidy --limit 5        # dry run on 5 files: shows the new names, renames nothing
sloth-shots tidy --limit 5 --apply  # happy? apply them (reuses the dry-run answers, no new API calls)
sloth-shots tidy --apply          # rename the whole backlog
sloth-shots watch                 # rename new screenshots as you take them
sloth-shots stats                 # how much effort you've saved
sloth-shots undo                  # put every original name back
```

Options:
- `--prefix "Cuplikan Layar"` matches screenshots from a non-English macOS. The option can be repeated.
- `--model` picks a different model. You can also set `SLOTH_MODEL`.

## How it works

1. **Find screenshots.** It only touches image files whose names start with `Screenshot` or `Screen Shot`. Renamed files no longer match, so nothing is processed twice.
2. **Shrink.** It downscales each image to 1024px with macOS's built-in `sips`. A 1 MB retina PNG becomes about 80 KB before upload.
3. **Name.** It asks Gemma 4 (`google/gemma-4-31b-it`, falling back to the 26B model and then the free tier) for a 3–6 word, hyphenated description, then sanitizes the answer into a safe filename. The original date stays as a prefix, so files still sort by date.
4. **Rename safely.**
   - It never overwrites a file; name collisions get `-2`, `-3`, and so on.
   - If the model returns nothing usable, the file is left alone.
   - Every rename is logged to `.sloth-shots.jsonl` in the folder, so `undo` can reverse it.
   - `tidy` is a dry run until you add `--apply`.

`watch` polls the folder every 2 seconds and waits until a file stops growing before touching it. It needs no dependencies.

## Why an open-weight model

Gemma's weights are public. The model that names your screenshots today will still exist and still behave the same way next year. Nobody can deprecate it or quietly retrain it under you. Because the weights are open, a dozen providers compete to serve it, which is why naming 200 screenshots costs under one cent. When the free pool is busy, sloth-shots falls through to another model instead of waiting.

Note: in this version, screenshots are sent through OpenRouter to whichever provider serves the model. Don't point it at a folder of things you wouldn't upload. Since the weights are open, the same model can run fully offline; a local backend is the obvious next step.

## License

MIT, see [LICENSE](LICENSE).
