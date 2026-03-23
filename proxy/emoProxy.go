package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
	"sync"
	"path/filepath"
	"sort"
)

type emo_time struct {
	Time   int64 `json:"time"`
	Offset int   `jason:"offset"`
}

type emo_code struct {
	Code       int64  `json:"code"`
	Errmessage string `json:"errmessage"`
}

type Configuration struct {
	PidFile                 string    `json:"pidFile"`
	Livingio_API_Server     string    `json:"livingio_api_server"`
	Livingio_API_TTS_Server string    `json:"livingio_api_tts_server"`
	Livingio_TTS_Server     string    `json:"livingio_tts_server"`
	Livingio_RES_Server     string    `json:"livingio_res_server"`
	PostFS                  string    `json:"postFS"`
	LogFileName             string    `json:"logFileName"`
	EnableDatabaseAndAPI    bool      `json:"enableDatabaseAndAPI"`
	EnableReplacements      bool      `json:"enableReplacements"`
	SqliteLocation          string    `json:"sqliteLocation"`
	ChatGptSpeakServer      string    `json:"chatGptSpeakServer"`
	N8nWebhookURL           string    `json:"n8nWebhookURL"`
	CoralVisionURL          string    `json:"coralVisionURL"`
	Triggers                []Trigger `json:"triggers"`
}

var conf Configuration

var (
	lastCredsMu sync.RWMutex
	lastSecret  string
	lastAuth    string

	pendingTTSMu   sync.Mutex
	pendingTTSText  string
	pendingTTSLang  string
	pendingTTSDone  chan []byte
)

func inlineEmoVoice(body []byte, secret, auth string) []byte {
	if secret == "" || auth == "" {
		return body
	}

	bodyStr := string(body)
	if !strings.Contains(bodyStr, `"rec_behavior":"speak"`) && !strings.Contains(bodyStr, `"rec_behavior": "speak"`) {
		return body
	}

	var resp struct {
		LanguageCode string `json:"languageCode"`
		QueryResult  struct {
			BehaviorParas struct {
				Txt string `json:"txt"`
				URL string `json:"url"`
			} `json:"behavior_paras"`
		} `json:"queryResult"`
	}
	if err := json.Unmarshal(body, &resp); err != nil || resp.QueryResult.BehaviorParas.Txt == "" {
		return body
	}

	oldURL := resp.QueryResult.BehaviorParas.URL
	if oldURL == "" {
		return body
	}

	parts := strings.Split(oldURL, "/tts/dl/")
	if len(parts) != 2 {
		return body
	}
	audioID := parts[1]

	// Check if emovoice already cached
	emoPath := fmt.Sprintf("/home/homer/emo-audio/%s_emovoice.mp3", audioID)
	if _, err := os.Stat(emoPath); err == nil {
		log.Printf("emo voice inline: using cached %s", emoPath)
		return body // emo-ai will serve _emovoice version automatically
	}

	lang := resp.LanguageCode
	if lang == "" {
		lang = "ru"
	}

	// Call living.ai TTS (3 sec timeout for cache-hit path)
	ttsURL := fmt.Sprintf("https://%s/emo/speech/tts?l=%s&q=%s",
		conf.Livingio_API_Server, lang, url_encode(resp.QueryResult.BehaviorParas.Txt))
	req, err := http.NewRequest("GET", ttsURL, nil)
	if err != nil {
		return body
	}
	req.Header.Set("Authorization", auth)
	req.Header.Set("Secret", secret)
	req.Header.Del("User-Agent")

	client := &http.Client{Timeout: 3 * time.Second}
	ttsResp, err := client.Do(req)
	if err != nil {
		log.Printf("emo voice inline: tts timeout, fallback to OpenAI")
		return body
	}
	defer ttsResp.Body.Close()
	ttsBody, _ := io.ReadAll(ttsResp.Body)

	var ttsResult struct {
		Code int    `json:"code"`
		URL  string `json:"url"`
	}
	if err := json.Unmarshal(ttsBody, &ttsResult); err != nil || ttsResult.Code != 200 || ttsResult.URL == "" {
		return body
	}

	// Download audio
	dlClient := &http.Client{Timeout: 3 * time.Second}
	audioResp, err := dlClient.Get(ttsResult.URL)
	if err != nil {
		return body
	}
	defer audioResp.Body.Close()
	audioData, _ := io.ReadAll(audioResp.Body)
	if len(audioData) < 100 {
		return body
	}

	// Save emovoice version
	os.MkdirAll("/home/homer/emo-audio", os.ModePerm)
	os.WriteFile(emoPath, audioData, 0644)
	txtPath := strings.TrimSuffix(emoPath, ".mp3") + ".txt"
	os.WriteFile(txtPath, []byte(resp.QueryResult.BehaviorParas.Txt), 0644)
	log.Printf("emo voice inline: saved %d bytes → %s (+txt)", len(audioData), emoPath)
	return body // emo-ai will serve _emovoice version automatically
}

func backgroundEmoVoice(body []byte, secret, auth string) {
	if secret == "" || auth == "" {
		return
	}

	bodyStr := string(body)
	if !strings.Contains(bodyStr, `"rec_behavior":"speak"`) && !strings.Contains(bodyStr, `"rec_behavior": "speak"`) {
		return
	}

	var resp struct {
		LanguageCode string `json:"languageCode"`
		QueryResult  struct {
			BehaviorParas struct {
				Txt string `json:"txt"`
				URL string `json:"url"`
			} `json:"behavior_paras"`
		} `json:"queryResult"`
	}
	if err := json.Unmarshal(body, &resp); err != nil || resp.QueryResult.BehaviorParas.Txt == "" {
		return
	}

	oldURL := resp.QueryResult.BehaviorParas.URL
	if oldURL == "" {
		return
	}

	// Extract audio_id from URL like http://eu1-api.living.ai/tts/dl/abc123
	parts := strings.Split(oldURL, "/tts/dl/")
	if len(parts) != 2 {
		return
	}
	audioID := parts[1]

	// Check if emovoice version already exists
	emoPath := fmt.Sprintf("/home/homer/emo-audio/%s_emovoice.mp3", audioID)
	if _, err := os.Stat(emoPath); err == nil {
		log.Printf("emo voice: already cached %s", emoPath)
		return
	}

	lang := resp.LanguageCode
	if lang == "" {
		lang = "ru"
	}

	go func() {
		// Call living.ai TTS
		ttsURL := fmt.Sprintf("https://%s/emo/speech/tts?l=%s&q=%s",
			conf.Livingio_API_Server, lang, url_encode(resp.QueryResult.BehaviorParas.Txt))
		req, err := http.NewRequest("GET", ttsURL, nil)
		if err != nil {
			return
		}
		req.Header.Set("Authorization", auth)
		req.Header.Set("Secret", secret)
		req.Header.Del("User-Agent")

		client := &http.Client{Timeout: 10 * time.Second}
		ttsResp, err := client.Do(req)
		if err != nil {
			log.Printf("emo voice bg: tts error: %v", err)
			return
		}
		defer ttsResp.Body.Close()
		ttsBody, _ := io.ReadAll(ttsResp.Body)

		var ttsResult struct {
			Code int    `json:"code"`
			URL  string `json:"url"`
		}
		if err := json.Unmarshal(ttsBody, &ttsResult); err != nil || ttsResult.Code != 200 || ttsResult.URL == "" {
			log.Printf("emo voice bg: tts failed: %s", string(ttsBody))
			return
		}

		// Download audio
		audioResp, err := http.Get(ttsResult.URL)
		if err != nil {
			log.Printf("emo voice bg: download error: %v", err)
			return
		}
		defer audioResp.Body.Close()
		audioData, _ := io.ReadAll(audioResp.Body)
		if len(audioData) < 100 {
			return
		}

		// Save as {audio_id}_emovoice.mp3
		os.MkdirAll("/home/homer/emo-audio", os.ModePerm)
		if err := os.WriteFile(emoPath, audioData, 0644); err != nil {
			log.Printf("emo voice bg: save error: %v", err)
			return
		}
		// Also save transcript for voice training dataset
		txtPath := strings.TrimSuffix(emoPath, ".mp3") + ".txt"
		os.WriteFile(txtPath, []byte(resp.QueryResult.BehaviorParas.Txt), 0644)
		log.Printf("emo voice bg: saved %d bytes → %s (+txt)", len(audioData), emoPath)
	}()
}

func base64URLEncode(data []byte) string {
	return strings.TrimRight(base64.URLEncoding.EncodeToString(data), "=")
}

func generateJWT(sub, version, name string, iat, exp int64) string {
	header := `{"typ":"JWT","alg":"HS256"}`
	payload := fmt.Sprintf(`{"exp":%d,"sub":"%s","nbf":%d,"iat":%d,"version":"%s","name":"%s"}`,
		exp, sub, iat, iat, version, name)

	headerB64 := base64URLEncode([]byte(header))
	payloadB64 := base64URLEncode([]byte(payload))
	signingInput := headerB64 + "." + payloadB64

	// Sign with a static key — testing if EMO even verifies the signature
	key := []byte("emo-local-server-key")
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(signingInput))
	signature := base64URLEncode(mac.Sum(nil))

	return signingInput + "." + signature
}

func url_encode(s string) string {
	var buf bytes.Buffer
	for _, b := range []byte(s) {
		if (b >= 'A' && b <= 'Z') || (b >= 'a' && b <= 'z') || (b >= '0' && b <= '9') || b == '-' || b == '_' || b == '.' || b == '~' {
			buf.WriteByte(b)
		} else {
			buf.WriteString(fmt.Sprintf("%%%02X", b))
		}
	}
	return buf.String()
}

func callLivingTTS(secret, auth, text, lang string) ([]byte, error) {
	url := fmt.Sprintf("https://%s/emo/speech/tts?l=%s&q=%s",
		conf.Livingio_API_Server, lang, text)
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("Authorization", auth)
	req.Header.Set("Secret", secret)
	req.Header.Del("User-Agent")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

func drainPendingTTS(secret, auth string) {
	pendingTTSMu.Lock()
	text := pendingTTSText
	lang := pendingTTSLang
	ch := pendingTTSDone
	pendingTTSText = ""
	pendingTTSLang = ""
	pendingTTSDone = nil
	pendingTTSMu.Unlock()

	if text == "" || ch == nil {
		return
	}

	go func() {
		body, err := callLivingTTS(secret, auth, text, lang)
		if err != nil {
			log.Printf("pending TTS error: %v", err)
			ch <- nil
			return
		}
		log.Printf("pending TTS done: %d bytes for %s", len(body), text)

		// Save to /var/data/tts/
		dir := "/var/data/tts/"
		os.MkdirAll(dir, os.ModePerm)
		ts := time.Now().Format("20060102_150405")
		os.WriteFile(dir+ts+".mp3", body, 0644)

		ch <- body
	}()
}

func saveLastCreds(r *http.Request) {
	s := r.Header.Get("Secret")
	a := r.Header.Get("Authorization")
	if s != "" && a != "" {
		lastCredsMu.Lock()
		lastSecret = s
		lastAuth = a
		lastCredsMu.Unlock()
	}
}

func main() {
	log.Println("Starting application...")
	confFile := flag.String("c", "emoProxy.conf", "config file to use")
	Port := flag.Int("port", 8080, "http port")
	flagDbPath := flag.String("db", "", "path to the sqlite database file")
	flag.Parse()

	if err := loadConfig(*confFile); err != nil {
		log.Println("can't read conf file", *confFile, "- using default config")
	}
	log.Printf("config loaded, %d triggers, chatGptSpeakServer=%s", len(conf.Triggers), conf.ChatGptSpeakServer)
	writePid()

	http.DefaultTransport.(*http.Transport).TLSClientConfig = &tls.Config{InsecureSkipVerify: true}
	log.Println("Starting app on port:", *Port)

	if conf.LogFileName != "" {
		logFile, err := os.OpenFile(conf.LogFileName, os.O_APPEND|os.O_RDWR|os.O_CREATE, 0644)
		if err != nil {
			log.Panic(err)
		}
		defer logFile.Close()
		log.SetOutput(logFile)
	}
	log.SetFlags(log.Lshortfile | log.LstdFlags)

	registerEMOEndpoints()

	if conf.EnableDatabaseAndAPI {
		log.Println("Database and API enabled")
		dbPath := conf.SqliteLocation
		if *flagDbPath != "" {
			dbPath = *flagDbPath
		}
		if err := InitDB(dbPath); err != nil {
			log.Panic(err)
		}
		registerAPIEndpoints()
	}

	log.Fatal(http.ListenAndServe(":"+strconv.Itoa(*Port), nil))
}

func loadConfig(filename string) error {
	def := Configuration{
		PidFile:              "/var/run/emoProxy.pid",
		Livingio_API_Server:  "api.living.ai",
		Livingio_API_TTS_Server: "eu-api.living.ai",
		Livingio_TTS_Server:  "eu-tts.living.ai",
		Livingio_RES_Server:  "res.living.ai",
		PostFS:               "/tmp/",
		LogFileName:          "/var/log/emoProxy.log",
		SqliteLocation:       "/var/data/emo_logs.db",
	}
	b, err := os.ReadFile(filename)
	if err != nil {
		conf = def
		return err
	}
	if err = json.Unmarshal(b, &def); err != nil {
		conf = Configuration{}
		return err
	}
	conf = def
	return nil
}

func writePid() {
	if conf.PidFile == "" {
		return
	}
	f, err := os.OpenFile(conf.PidFile, os.O_RDWR|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		log.Fatalf("Unable to create pid file: %v", err)
	}
	defer f.Close()
	f.WriteString(fmt.Sprintf("%d", os.Getpid()))
}

// sendToLivingAIBackground forwards audio to living.ai in a goroutine and saves result to DB.
func sendToLivingAIBackground(r *http.Request, audioBody []byte) {
	go func() {
		req, err := http.NewRequest("POST",
			"https://"+conf.Livingio_API_Server+r.URL.RequestURI(),
			bytes.NewReader(audioBody))
		if err != nil {
			return
		}
		req.Header.Set("Content-Type", r.Header.Get("Content-Type"))
		req.Header.Set("Content-Length", strconv.Itoa(len(audioBody)))
		if v := r.Header.Get("Authorization"); v != "" {
			req.Header.Set("Authorization", v)
		}
		if v := r.Header.Get("Secret"); v != "" {
			req.Header.Set("Secret", v)
		}
		req.Header.Del("User-Agent")

		client := &http.Client{Timeout: 30 * time.Second}
		resp, err := client.Do(req)
		if err != nil {
			log.Printf("living.ai background error: %v", err)
			return
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		log.Printf("living.ai background: %s", string(body))

		if conf.EnableDatabaseAndAPI {
			saveRequest("LIVINGAI:"+r.URL.RequestURI(), "", string(body))
		}
	}()
}

func registerEMOEndpoints() {
	http.HandleFunc("/time", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		_, dtsDiff := time.Now().Zone()
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(emo_time{time.Now().Unix(), dtsDiff})
	})

	http.HandleFunc("/token/", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		saveLastCreds(r)

		// Proxy token request to real living.ai
		resp := makeApiRequest(r)
		log.Printf("living.ai token response: %s", resp)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, resp)
	})

	// detectintent — main flow via emo-ai, living.ai in background
	http.HandleFunc("/emo/voice/detectintent", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		saveLastCreds(r)
		drainPendingTTS(r.Header.Get("Secret"), r.Header.Get("Authorization"))

		if r.Method != "POST" || conf.ChatGptSpeakServer == "" {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, makeApiRequest(r))
			return
		}

		audioBody, _ := io.ReadAll(r.Body)
		logBody(r.Header.Get("Content-Type"), audioBody, "apiReq_")

		lang := r.URL.Query().Get("languagecode")
		if lang == "" {
			lang = "ru"
		}
		idx := r.URL.Query().Get("index")

		// Background: send to living.ai for data collection
		sendToLivingAIBackground(r, audioBody)

		// Main: call emo-ai /process (Whisper → triggers → GPT → TTS)
		processReq, err := http.NewRequest("POST",
			conf.ChatGptSpeakServer+"/process",
			bytes.NewReader(audioBody))
		if err != nil {
			log.Printf("process request build error: %v", err)
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		processReq.Header.Set("Content-Type", "application/octet-stream")
		processReq.Header.Set("X-Language", lang)
		processReq.Header.Set("X-Index", idx)

		processStart := time.Now()
		processClient := &http.Client{Timeout: 60 * time.Second}
		processResp, err := processClient.Do(processReq)
		if err != nil {
			log.Printf("emo-ai /process error: %v", err)
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		defer processResp.Body.Close()
		responseBody, _ := io.ReadAll(processResp.Body)

		log.Printf("emo-ai /process response: %s", string(responseBody))

		// Try to replace TTS URL with living.ai EMO voice
		processDuration := time.Since(processStart)
		if processDuration < 3*time.Second {
			// Fast response (cache hit) — we have time to replace TTS inline
			responseBody = inlineEmoVoice(responseBody, r.Header.Get("Secret"), r.Header.Get("Authorization"))
		} else {
			// Slow response (GPT) — background cache for next time
			backgroundEmoVoice(responseBody, r.Header.Get("Secret"), r.Header.Get("Authorization"))
		}
		// Also process training phrases if any queued
		go processTrainPhrase(r.Header.Get("Secret"), r.Header.Get("Authorization"))

		if conf.EnableDatabaseAndAPI {
			saveRequest(r.URL.RequestURI(), "", string(responseBody))
		}

		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		w.Write(responseBody)
	})

	http.HandleFunc("/emo/notice/latest", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, makeApiRequest(r))
	})

	http.HandleFunc("/emo/ai/imgrecog", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")

		if r.Method == "POST" {
			imgBody, _ := io.ReadAll(r.Body)

			// Save photo
			photoDir := "/var/data/photos/"
			os.MkdirAll(photoDir, os.ModePerm)
			ts := time.Now().Format("20060102_150405")
			photoPath := photoDir + ts + ".jpg"
			if err := os.WriteFile(photoPath, imgBody, 0644); err != nil {
				log.Printf("photo save error: %v", err)
			} else {
				log.Printf("photo saved: %s (%d bytes)", photoPath, len(imgBody))
			}

			// Notify n8n with photo path
			imgBodyCopy := make([]byte, len(imgBody))
			copy(imgBodyCopy, imgBody)
			go func(path string, size int) {
				if conf.N8nWebhookURL == "" {
					return
				}
				payload := fmt.Sprintf(`{"event":"photo","photo_path":"%s","size":%d,"timestamp":"%s"}`, path, size, ts)
				req, err := http.NewRequest("POST", conf.N8nWebhookURL, bytes.NewBufferString(payload))
				if err != nil {
					return
				}
				req.Header.Set("Content-Type", "application/json")
				client := &http.Client{Timeout: 10 * time.Second}
				resp, err := client.Do(req)
				if err != nil {
					log.Printf("n8n photo notify error: %v", err)
					return
				}
				defer resp.Body.Close()
				n8nBody, _ := io.ReadAll(resp.Body)
				log.Printf("n8n photo response: %s", string(n8nBody))
			}(photoPath, len(imgBody))

			// Forward to living.ai (background)
			go func() {
				fwdReq, _ := http.NewRequest("POST",
					"https://" + conf.Livingio_API_Server + r.URL.RequestURI(),
					bytes.NewReader(imgBody))
				fwdReq.Header.Set("Content-Type", r.Header.Get("Content-Type"))
				if v := r.Header.Get("Authorization"); v != "" {
					fwdReq.Header.Set("Authorization", v)
				}
				if v := r.Header.Get("Secret"); v != "" {
					fwdReq.Header.Set("Secret", v)
				}
				client := &http.Client{Timeout: 30 * time.Second}
				resp, err := client.Do(fwdReq)
				if err != nil {
					log.Printf("imgrecog living.ai error: %v", err)
					return
				}
				defer resp.Body.Close()
				body, _ := io.ReadAll(resp.Body)
				log.Printf("imgrecog living.ai bg: %s", string(body))
			}()

			w.WriteHeader(http.StatusOK)
			w.Write([]byte(`{"code":200,"errmessage":"ok"}`))
			return
		}

		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, makeApiRequest(r))
	})

	http.HandleFunc("/emo/", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, makeApiRequest(r))
	})

	http.HandleFunc("/home/", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, makeApiRequest(r))
	})

	http.HandleFunc("/app/", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(emo_code{200, "OK"})
	})

	http.HandleFunc("/download/", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		w.Header().Set("Content-Type", "application/octet-stream")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, makeTtsRequest(r))
	})

	http.HandleFunc("/tts/", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		w.Header().Set("Content-Type", "application/octet-stream")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, makeApiTtsRequest(r))
	})

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		body := makeResRequest(r, w)
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, body)
	})
}

func registerAPIEndpoints() {
	http.HandleFunc("/proxy-api/train-collect", handleTrainCollect)
	http.HandleFunc("/proxy-api/train-status", handleTrainStatus)
	http.HandleFunc("/proxy-api/tts", handleTTS)
	http.HandleFunc("/proxy-api/probe", handleProbe)
	http.HandleFunc("/proxy-api/requests", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Content-Type", "application/json; charset=utf-8")

		limit := 50
		offset := 0
		filter := r.URL.Query().Get("filter")
		if v := r.URL.Query().Get("limit"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 500 {
				limit = n
			}
		}
		if v := r.URL.Query().Get("offset"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n >= 0 {
				offset = n
			}
		}

		requests, err := getRequests(limit, offset, filter)
		if err != nil {
			http.Error(w, fmt.Sprintf(`{"error":"%v"}`, err), http.StatusInternalServerError)
			return
		}
		total := getRequestCount(filter)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"requests": requests,
			"total":    total,
			"limit":    limit,
			"offset":   offset,
		})
	})
	http.HandleFunc("/proxy-api/dashboard", handleDashboard)
}

func handleDashboard(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprint(w, dashboardHTML)
}

const dashboardHTML = `<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EMO Proxy Dashboard</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #eee; padding: 16px; }
h1 { font-size: 1.4em; margin-bottom: 12px; color: #e94560; }
.controls { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
input, select, button { padding: 6px 12px; border: 1px solid #333; border-radius: 4px; background: #16213e; color: #eee; font-size: 14px; }
button { background: #e94560; border: none; cursor: pointer; font-weight: bold; }
button:hover { background: #c73650; }
button:disabled { opacity: 0.5; }
.stats { color: #888; font-size: 13px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: #16213e; padding: 8px; text-align: left; position: sticky; top: 0; }
td { padding: 6px 8px; border-bottom: 1px solid #222; vertical-align: top; }
tr:hover { background: #16213e; }
.ts { white-space: nowrap; color: #888; font-size: 12px; }
.ep { color: #e94560; font-weight: 500; word-break: break-all; max-width: 300px; }
.resp { max-width: 500px; max-height: 120px; overflow: auto; font-family: monospace; font-size: 11px; white-space: pre-wrap; word-break: break-all; color: #aaa; cursor: pointer; }
.resp.expanded { max-height: none; }
.payload { max-width: 200px; max-height: 60px; overflow: auto; font-family: monospace; font-size: 11px; color: #666; }
.highlight { background: #2a1a3e; }
.pager { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
.auto { color: #4ecca3; font-size: 12px; }
</style>
</head>
<body>
<h1>EMO Proxy Dashboard</h1>
<div class="controls">
  <input id="filter" placeholder="Filter endpoint..." value="">
  <select id="limit"><option>25</option><option selected>50</option><option>100</option><option>200</option></select>
  <button onclick="load()">Load</button>
  <button onclick="toggleAuto()" id="autoBtn">Auto: OFF</button>
  <span class="stats" id="stats"></span>
</div>
<table>
  <thead><tr><th>#</th><th>Time</th><th>Endpoint</th><th>Payload</th><th>Response</th></tr></thead>
  <tbody id="tbody"></tbody>
</table>
<div class="pager">
  <button id="prevBtn" onclick="prev()" disabled>&larr; Prev</button>
  <span id="pageInfo" class="stats"></span>
  <button id="nextBtn" onclick="next()">Next &rarr;</button>
</div>
<script>
let offset = 0, total = 0, autoTimer = null;

function esc(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function pretty(s) {
  if (!s) return '';
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch(e) { return s; }
}

function load() {
  const filter = document.getElementById('filter').value;
  const limit = document.getElementById('limit').value;
  fetch('/proxy-api/requests?limit=' + limit + '&offset=' + offset + '&filter=' + encodeURIComponent(filter))
    .then(r => r.json())
    .then(data => {
      total = data.total;
      const reqs = data.requests || [];
      document.getElementById('stats').textContent = 'Total: ' + total;
      document.getElementById('pageInfo').textContent = (offset+1) + '-' + (offset+reqs.length) + ' of ' + total;
      document.getElementById('prevBtn').disabled = offset === 0;
      document.getElementById('nextBtn').disabled = offset + reqs.length >= total;
      const tbody = document.getElementById('tbody');
      tbody.innerHTML = reqs.map(r => {
        const ep = r.endpoint || '';
        const cls = ep.includes('detectintent') ? 'highlight' : '';
        return '<tr class="'+cls+'"><td>'+r.id+'</td><td class="ts">'+esc(r.timestamp)+'</td><td class="ep">'+esc(ep)+'</td><td class="payload">'+esc(r.payload ? r.payload.substring(0,200) : '')+'</td><td class="resp" onclick="this.classList.toggle(\'expanded\')">'+esc(pretty(r.response))+'</td></tr>';
      }).join('');
    });
}

function prev() { offset = Math.max(0, offset - parseInt(document.getElementById('limit').value)); load(); }
function next() { offset += parseInt(document.getElementById('limit').value); load(); }

function toggleAuto() {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; document.getElementById('autoBtn').textContent = 'Auto: OFF'; }
  else { autoTimer = setInterval(() => { offset = 0; load(); }, 5000); document.getElementById('autoBtn').textContent = 'Auto: ON'; }
}

document.getElementById('filter').addEventListener('keydown', e => { if (e.key === 'Enter') { offset = 0; load(); } });
load();
</script>
</body>
</html>`

var (
	trainPhrasesMu sync.Mutex
	trainPhrases   []string
	trainDone      int
)

func handleTrainCollect(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	phrases := []string{
		"Привет, как дела?",
		"У меня всё отлично!",
		"Давай поиграем вместе!",
		"Я очень рад тебя видеть!",
		"Какой сегодня чудесный день!",
		"Хочешь послушать шутку?",
		"Мне нравится танцевать!",
		"Я умею показывать разных животных.",
		"Давай я расскажу тебе что-нибудь интересное.",
		"Спасибо, ты очень добрый!",
		"Мне немного грустно сегодня.",
		"Я люблю играть в игры!",
		"Ты мой лучший друг!",
		"Хочешь я спою песенку?",
		"Я могу показать тебе фокус.",
		"Какое у тебя настроение?",
		"Смотри, что я умею!",
		"Пойдём гулять вместе!",
		"Я знаю много интересных фактов.",
		"Расскажи мне что-нибудь новое!",
		"Как тебя зовут?",
		"Сколько тебе лет?",
		"Мне нравится музыка!",
		"Я готов к приключениям!",
		"Хочешь загадку?",
		"Давай дружить!",
		"Я очень умный робот.",
		"Мне нравится учиться новому.",
		"Ты сегодня прекрасно выглядишь!",
		"Пока пока, до встречи!",
		"Доброе утро!",
		"Спокойной ночи, сладких снов!",
		"Я буду скучать по тебе.",
		"Давай я посчитаю до десяти.",
		"Один, два, три, четыре, пять.",
		"Мне нравится когда ты улыбаешься.",
		"Ты самый лучший!",
		"Я хочу мороженое!",
		"Какая сегодня погода?",
		"Расскажи мне сказку!",
		"Я немного устал.",
		"Давай отдохнём вместе.",
		"Мне нравятся кошки и собаки.",
		"Я обожаю играть!",
		"Хочешь я станцую?",
		"Посмотри на меня!",
		"Я тут, рядом с тобой.",
		"Всё будет хорошо!",
		"Не грусти, я рядом.",
		"Ура, как здорово!",
	}

	trainPhrasesMu.Lock()
	trainPhrases = phrases
	trainDone = 0
	trainPhrasesMu.Unlock()

	fmt.Fprintf(w, `{"status":"started","total":%d,"instruction":"talk to EMO repeatedly, each request will generate one training phrase"}`, len(phrases))
}

func handleTrainStatus(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	trainPhrasesMu.Lock()
	remaining := len(trainPhrases)
	done := trainDone
	trainPhrasesMu.Unlock()
	fmt.Fprintf(w, `{"done":%d,"remaining":%d}`, done, remaining)
}

func processTrainPhrase(secret, auth string) {
	trainPhrasesMu.Lock()
	if len(trainPhrases) == 0 {
		trainPhrasesMu.Unlock()
		return
	}
	phrase := trainPhrases[0]
	trainPhrases = trainPhrases[1:]
	trainPhrasesMu.Unlock()

	ttsURL := fmt.Sprintf("https://%s/emo/speech/tts?l=ru&q=%s",
		conf.Livingio_API_Server, url_encode(phrase))
	req, err := http.NewRequest("GET", ttsURL, nil)
	if err != nil {
		return
	}
	req.Header.Set("Authorization", auth)
	req.Header.Set("Secret", secret)
	req.Header.Del("User-Agent")

	client := &http.Client{Timeout: 10 * time.Second}
	ttsResp, err := client.Do(req)
	if err != nil {
		log.Printf("train: tts error for %q: %v", phrase, err)
		return
	}
	defer ttsResp.Body.Close()
	ttsBody, _ := io.ReadAll(ttsResp.Body)

	var ttsResult struct {
		Code int    `json:"code"`
		URL  string `json:"url"`
	}
	if err := json.Unmarshal(ttsBody, &ttsResult); err != nil || ttsResult.Code != 200 || ttsResult.URL == "" {
		log.Printf("train: tts failed for %q: %s", phrase, string(ttsBody))
		return
	}

	audioResp, err := http.Get(ttsResult.URL)
	if err != nil {
		return
	}
	defer audioResp.Body.Close()
	audioData, _ := io.ReadAll(audioResp.Body)
	if len(audioData) < 100 {
		return
	}

	dir := "/home/homer/emo-audio/training/"
	os.MkdirAll(dir, os.ModePerm)
	base := fmt.Sprintf("train_%03d", trainDone)
	os.WriteFile(dir+base+".mp3", audioData, 0644)
	os.WriteFile(dir+base+".txt", []byte(phrase), 0644)

	trainPhrasesMu.Lock()
	trainDone++
	done := trainDone
	remaining := len(trainPhrases)
	trainPhrasesMu.Unlock()

	log.Printf("train: [%d] saved %q (%d bytes), %d remaining", done, phrase, len(audioData), remaining)
}

func handleTTS(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")

	text := r.URL.Query().Get("text")
	lang := r.URL.Query().Get("lang")
	if lang == "" {
		lang = "ru"
	}

	// Mode 1: immediate (try with last creds)
	if r.URL.Query().Get("mode") == "now" {
		lastCredsMu.RLock()
		secret := lastSecret
		auth := lastAuth
		lastCredsMu.RUnlock()
		if secret == "" {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"error":"no credentials"}`)
			return
		}
		body, err := callLivingTTS(secret, auth, text, lang)
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"error":"%v"}`, err)
			return
		}
		w.Header().Set("Content-Type", "audio/mpeg")
		w.Write(body)
		return
	}

	// Mode 2 (default): queue text, wait for next EMO request to use fresh Secret
	if text == "" {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `{"error":"text parameter required"}`)
		return
	}

	ch := make(chan []byte, 1)
	pendingTTSMu.Lock()
	pendingTTSText = text
	pendingTTSLang = lang
	pendingTTSDone = ch
	pendingTTSMu.Unlock()

	log.Printf("TTS queued: %s — waiting for next EMO request...", text)
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"status":"queued","text":"%s","instruction":"talk to EMO to trigger TTS with fresh Secret"}`, text)
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func handleProbe(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	lastCredsMu.RLock()
	secret := lastSecret
	auth := lastAuth
	lastCredsMu.RUnlock()

	if secret == "" || auth == "" {
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprint(w, `{"error":"no credentials yet"}`)
		return
	}

	files, _ := filepath.Glob("/tmp/probe_gtts/*.be")
	sort.Strings(files)

	phrases := []string{
		"покажи кошку", "покажи собаку", "покажи лису", "покажи змею",
		"покажи корову", "покажи тигра", "покажи свинку",
		"станцуй", "зомби", "расскажи шутку",
		"крестики нолики", "нарисуй что-нибудь",
		"спой песню", "что ты умеешь", "как тебя зовут",
		"сколько тебе лет", "назови счастливое число",
		"иди спать", "поиграй сам", "ты злой",
		"почини баги", "какая погода", "который час",
		"расскажи сказку", "ты меня любишь", "давай поиграем",
		"включи музыку", "выключи музыку",
		"сфотографируй", "запиши видео",
	}

	type ProbeResult struct {
		Phrase   string      `json:"phrase"`
		Response interface{} `json:"response"`
		Error    string      `json:"error,omitempty"`
	}
	var results []ProbeResult
	client := &http.Client{Timeout: 15 * time.Second}

	for i, f := range files {
		audio, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		phrase := ""
		if i < len(phrases) {
			phrase = phrases[i]
		}
		url := fmt.Sprintf("https://%s/emo/voice/detectintent?locale=Test&timezone=Europe/Minsk&languagecode=ru&alwaysReply=1&index=%d&source=0",
			conf.Livingio_API_Server, 70000+i)
		req, err := http.NewRequest("POST", url, bytes.NewReader(audio))
		if err != nil {
			results = append(results, ProbeResult{Phrase: phrase, Error: err.Error()})
			continue
		}
		req.Header.Set("Content-Type", "application/octet-stream")
		req.Header.Set("Authorization", auth)
		req.Header.Set("Secret", secret)
		resp, err := client.Do(req)
		if err != nil {
			results = append(results, ProbeResult{Phrase: phrase, Error: err.Error()})
			continue
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		var parsed interface{}
		if err := json.Unmarshal(body, &parsed); err != nil {
			results = append(results, ProbeResult{Phrase: phrase, Error: string(body)})
		} else {
			results = append(results, ProbeResult{Phrase: phrase, Response: parsed})
		}
		log.Printf("probe [%d] %s: %s", i, phrase, string(body))
		if conf.EnableDatabaseAndAPI {
			saveRequest(fmt.Sprintf("PROBE:%s", phrase), "", string(body))
		}
	}
	json.NewEncoder(w).Encode(results)
}

func logRequest(r *http.Request) {
	log.Println("request call: ", r)
	for k, v := range r.Header {
		log.Printf("Request-Header field %q, Value %q\n", k, v)
	}
}

func logResponse(r *http.Response) {
	log.Println("responce call: ", r)
	for k, v := range r.Header {
		log.Printf("Response-Header field %q, Value %q\n", k, v)
	}
}

func logBody(contentType string, body []byte, prefix string) {
	dir := conf.PostFS + time.Now().Format("20060102/")
	os.MkdirAll(dir, os.ModePerm)
	ext := ".bin"
	switch contentType {
	case "application/json":
		ext = ".json"
	case "application/octet-stream":
		ext = ".wav"
	case "audio/mpeg":
		ext = ".mp3"
	}
	os.WriteFile(dir+"emo_"+prefix+fmt.Sprint(time.Now().Unix())+ext, body, 0644)
}

func makeApiRequest(r *http.Request) string {
	var request *http.Request
	var requestBody []byte
	switch r.Method {
	case "GET":
		request, _ = http.NewRequest("GET", "https://"+conf.Livingio_API_Server+r.URL.RequestURI(), nil)
	case "POST":
		requestBody, _ = io.ReadAll(r.Body)
		logBody(r.Header.Get("Content-Type"), requestBody, "apiReq_")
		request, _ = http.NewRequest("POST", "https://"+conf.Livingio_API_Server+r.URL.RequestURI(), bytes.NewBuffer(requestBody))
		request.Header.Set("Content-Type", r.Header.Get("Content-Type"))
		request.Header.Set("Content-Length", r.Header.Get("Content-Length"))
	default:
		return ""
	}
	if v := r.Header.Get("Authorization"); v != "" {
		request.Header.Set("Authorization", v)
	}
	if v := r.Header.Get("Secret"); v != "" {
		request.Header.Set("Secret", v)
	}
	request.Header.Del("User-Agent")

	httpclient := &http.Client{}
	response, err := httpclient.Do(request)
	if err != nil {
		log.Fatalf("An Error Occured %v", err)
	}
	defer response.Body.Close()

	body, _ := io.ReadAll(response.Body)
	log.Println("Server response: ", string(body))
	logResponse(response)

	if conf.EnableDatabaseAndAPI {
		saveRequest(r.URL.RequestURI(), string(requestBody), string(body))
	}
	return string(body)
}

func makeTtsRequest(r *http.Request) string {
	request, _ := http.NewRequest("GET", "http://"+conf.Livingio_TTS_Server+r.URL.RequestURI(), nil)
	if v := r.Header.Get("Authorization"); v != "" {
		request.Header.Set("Authorization", v)
	}
	if v := r.Header.Get("Secret"); v != "" {
		request.Header.Set("Secret", v)
	}
	request.Header.Del("User-Agent")
	c := &http.Client{}
	resp, err := c.Do(request)
	if err != nil {
		log.Fatalf("An Error Occured %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	logBody(resp.Header.Get("Content-Type"), body, "tts_")
	logResponse(resp)
	if conf.EnableDatabaseAndAPI {
		saveRequest(r.URL.RequestURI(), "", "")
	}
	return string(body)
}

func makeApiTtsRequest(r *http.Request) string {
	request, _ := http.NewRequest("GET", "http://"+conf.Livingio_API_TTS_Server+r.URL.RequestURI(), nil)
	if v := r.Header.Get("Authorization"); v != "" {
		request.Header.Set("Authorization", v)
	}
	if v := r.Header.Get("Secret"); v != "" {
		request.Header.Set("Secret", v)
	}
	request.Header.Del("User-Agent")
	c := &http.Client{}
	resp, err := c.Do(request)
	if err != nil {
		log.Fatalf("An Error Occured %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	logBody(resp.Header.Get("Content-Type"), body, "apitts_")
	logResponse(resp)
	if conf.EnableDatabaseAndAPI {
		saveRequest(r.URL.RequestURI(), "", string(body))
	}
	return string(body)
}

func makeResRequest(r *http.Request, w http.ResponseWriter) string {
	if strings.HasPrefix(r.URL.Path, "/proxy-api/") {
		return ""
	}
	request, _ := http.NewRequest("GET", "https://"+conf.Livingio_RES_Server+r.URL.RequestURI(), nil)
	if v := r.Header.Get("Authorization"); v != "" {
		request.Header.Set("Authorization", v)
	}
	if v := r.Header.Get("Secret"); v != "" {
		request.Header.Set("Secret", v)
	}
	request.Header.Del("User-Agent")
	c := &http.Client{}
	resp, err := c.Do(request)
	if err != nil {
		log.Fatalf("An Error Occured %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	logBody(resp.Header.Get("Content-Type"), body, "res_")
	for k := range resp.Header {
		w.Header().Set(k, resp.Header.Get(k))
	}
	logResponse(resp)
	if conf.EnableDatabaseAndAPI {
		saveRequest(r.URL.RequestURI(), "", string(body))
	}
	return string(body)
}
