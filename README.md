# MATACS-TH-2026-02: AI proposal triage design

- **Full name:** ANUM KOUSAR
- **Reference:** MATACS-TH-2026-02
- **Time actually spent:** 5 hours -> 1 hours in creating the core files of the assessment and 4 hours in building the complete portfolio and make the detail testing.

## How I used AI tools

I used Claude to help build the working prototype, including its implementation. I used both GPT and Claude to review my approach, validate the scoring logic and design decisions, reviewing the prompt and help me express my answers clearly in the write-up.

**One place I disagreed with or corrected the AI:** 

The AI suggested using AI to score proposals as well. However, I chose deterministic scoring instead, using predefined criteria and rules in code. This reduces the risk of prompt injection influencing scores or rankings and keeps the evaluation consistent and explainable. I also included a human-in-the-loop review process so reviewers can verify the results and make informed final decisions.

## Files

- [WRITEUP.md](WRITEUP.md): answers to the five questions
- [diagram/flow.png](diagram/flow.png): process flow from upload to ranked output (AI steps, rule steps, human checkpoints)
- [PROMPTS.md](PROMPTS.md): the extraction prompt, exactly as sent
- [ranking.json](ranking.json): expected ranking of proposals A–E
- [prototype/](prototype/): optional code and a minimal review screen
  - [prototype/template/index.html](prototype/template/index.html): single-page view (upload, extract, rank, clear flags)
  - [prototype/.env.example](prototype/.env.example): environment variables (no keys committed)

## Running the Prototype

### 1. Clone the repository

```bash
git clone https://github.com/anum-349/matacs-th-2026-02.git
cd matacs-th-2026-02/prototype
```

### 2. Create a virtual environment

```bash
python -m venv venv
```

### 3. Activate the virtual environment

**Windows (PowerShell):**
```powershell
.\venv\Scripts\Activate.ps1
```

**Windows (Git Bash):**
```bash
source venv/Scripts/activate
```

**Linux/macOS:**
```bash
source venv/bin/activate
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Start the application

```bash
python app.py
```

### 6. Open the prototype

Open your browser and visit:

http://127.0.0.1:5000/