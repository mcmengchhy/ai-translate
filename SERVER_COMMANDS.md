# 🛠️ Server Management Commands (ការបញ្ជាបើក និងបិទ Server)

ឯកសារនេះផ្ទុកពាក្យបញ្ជា (Commands) ទាំងអស់សម្រាប់ដំណើរការ (Start), បិទ (Stop / Kill), និងពិនិត្យ (Status) ម៉ាស៊ីន Server ដោយខ្លួនឯងនៅក្នុង Terminal។

---

## ⚡ 1. វិធីសាមញ្ញបំផុត (Fastest Method via Scripts)

នៅក្នុង folder គម្រោង (`/Users/chhunmengchhy/Documents/the-py/tool/ai-translator`):

### 🟢 បើក Server (Start):
```bash
./run.sh
```
*ឬបើមិនទាន់មានសិទ្ធិ run សូមវាយ:*
```bash
chmod +x run.sh stop.sh
./run.sh
```

### 🔴 បិទ Server (Stop / Kill):
```bash
./stop.sh
```

---

## 💻 2. ពាក្យបញ្ជាផ្ទាល់ (Direct Commands in Terminal)

### 🟢 បើកដំណើរការ Server (Start Server):
```bash
cd /Users/chhunmengchhy/Documents/the-py/tool/ai-translator
./.venv/bin/python3 -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```
*បើអ្នកចូលចិត្ត activate venv ជាមុន:*
```bash
source .venv/bin/activate
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

### 🔴 បិទ / កម្ទេច Server ដែលកំពុងដំណើរការ (Kill / Stop Server):

#### ជម្រើសទី ១: បិទតាមរយៈ Port 8000 (លឿន និងមានប្រសិទ្ធភាពបំផុត)
```bash
lsof -ti :8000 | xargs kill -9
```

#### ជម្រើសទី ២: បិទគ្រប់ Process របស់ Uvicorn ទាំងអស់
```bash
pkill -9 -f uvicorn
```

#### ជម្រើសទី ៣: បើ Server កំពុងរត់ក្នុងផ្ទាំង Terminal ផ្ទាល់
- ចុចក្តារចុច: **`Ctrl + C`**

---

## 🔍 3. ពិនិត្យមើលស្ថានភាព Server (Check Server Status)

### ពិនិត្យមើលថាតើ Port 8000 កំពុងរត់ឬអត់:
```bash
lsof -i :8000
```
- បើឃើញបង្ហាញ `uvicorn` ឬ `Python` មានន័យថា Server កំពុងរត់។
- បើគ្មានចេញអ្វីសោះ មានន័យថា Server ត្រូវបានបិទស្អាត។

### ធ្វើតេស្តសាកល្បងហៅ API:
```bash
curl -s http://localhost:8000/api/video/voice-profiles
```

---

## 🌐 4. ចូលប្រើប្រាស់ Web App
នៅពេល Server បានបើកដំណើរការរួច សូមបើកកម្មវិធីរុករក (Chrome / Safari) ហើយចូលទៅកាន់៖
👉 **[http://localhost:8000](http://localhost:8000)**

---

## 📝 5. One-Liner Quick Cheat Sheet

| សកម្មភាព (Action) | ពាក្យបញ្ជា (Command) |
|---|---|
| **បើក Server** | `./run.sh` |
| **បិទ Server** | `./stop.sh` |
| **Kill Port 8000 ភ្លាមៗ** | `lsof -ti :8000 \| xargs kill -9` |
| **Kill All Uvicorn** | `pkill -9 -f uvicorn` |
| **ពិនិត្យ Port 8000** | `lsof -i :8000` |
| **បើកដំណើរការក្នុង Background** | `nohup ./.venv/bin/python3 -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload > server.log 2>&1 &` |

---

## ⚡ 6. របៀបបន្តដំណើរការវីដេអូវែង 7+ ម៉ោង (Resume Long 7-8h Video Jobs)

ប្រសិនបើ Server ត្រូវបិទ, Mac ចូល Sleep, ឬដាច់អ៊ីនធឺណិតពេលកំពុងរត់វីដេអូវែង 7-8 ម៉ោង៖
1. **មិនបាច់ចាប់ផ្តើមពី 0 ម៉ោងឡើងវិញទេ!** ទិន្នន័យ Transcription, Translation (12,000+ segments) និងសំឡេងដែលបានបង្កើតរួចត្រូវបានរក្សាទុកក្នុង Checkpoint ស្វ័យប្រវត្តិ។
2. បើក Server ឡើងវិញ: `./run.sh`
3. ចូលទៅកាន់ **[http://localhost:8000](http://localhost:8000)**
4. នៅត្រង់ប្រអប់ **"Previous Video Jobs"** ជ្រើសរើសយក Job ដែលបានផ្អាក រួចចុច **"Resume Job (បន្តពីកន្លែងចាស់)"**
5. ឬអ្នកអាច Resume តាមរយៈ Terminal ដោយវាយ:
   ```bash
   curl -X POST http://localhost:8000/api/video/job/a8e82864/resume -H "Content-Type: application/json" -d "{}"
   ```
   *(ជំនួស `a8e82864` ដោយ Job ID របស់អ្នក)*
6. ប្រព័ន្ធនឹងបន្តបង្កើតតែសំឡេងណាដែលសល់ រួចបញ្ចូលវីដេអូដោយស្វ័យប្រវត្តិ ក្នុងរយៈពេលលឿនបំផុត!

---

## ✂️ 7. របៀបកាត់ចែកវីដេអូជាវគ្គ ១ ម៉ោង (Cut / Split Video into 1h Parts)

សម្រាប់វីដេអូវែងៗ ៧-៨ ម៉ោង កម្មវិធីមានមុខងារកាត់ចែកជាវគ្គ ១ ម៉ោង (3600 វិនាទី) ស្វ័យប្រវត្តិ ដោយប្រើ Fast Stream Copy មិនបាត់បង់គុណភាព និងមិនចំណាយពេលយូរឡើយ (ត្រឹមតែ ១០-១៥ វិនាទីសម្រាប់វីដេអូ ៧ ម៉ោង!) ព្រមទាំងកាត់ Subtitle ខ្មែរ (.srt & .vtt) ត្រូវតាមវគ្គនីមួយៗទៀតផង៖

### តាមរយៈ Web UI:
1. នៅលើទំព័រដើម [http://localhost:8000](http://localhost:8000)
2. មុនពេលចុច Start Dubbing: ជ្រើសរើស **"Split Output (កាត់ជាវគ្គ): ✂️ 1 Hour / Part (3600s)"**
3. ឬបើវីដេអូ Render រួចហើយ (ឬនៅក្នុង CapCut Studio): ចុចប៊ូតុង **"✂️ Split 1h Parts (កាត់ជាវគ្គ)"**
4. ប្រព័ន្ធនឹងបង្ហាញផ្ទាំង **Episode Parts** ដែលអាច Play មើលវគ្គនីមួយៗ និង Download MP4 / SRT ដាច់ដោយឡែកពីគ្នាបានយ៉ាងងាយស្រួល!

### តាមរយៈ Terminal (One-line Command):
```bash
# កាត់វីដេអូក្នុង Job a8e82864 ជាវគ្គ ១ ម៉ោង (3600s):
curl -X POST http://localhost:8000/api/video/job/a8e82864/split -H "Content-Type: application/json" -d '{"split_duration": 3600, "target": "source"}'
```

