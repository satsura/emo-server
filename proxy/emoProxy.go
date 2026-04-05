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


// ── Living.ai sync STT + n8n brain ──────────────────────────────────────────

type n8nResponse struct {
	Action      string                 `json:"action"`
	Text        string                 `json:"text"`
	Voice       map[string]interface{} `json:"voice"`
	Animation   map[string]string      `json:"animation"`
	AsyncAction string                 `json:"async_action"`
	Query       string                 `json:"query"`
}

func sendToLivingAISync(r *http.Request, audioBody []byte) ([]byte, error) {
	req, err := http.NewRequest("POST",
		"https://"+conf.Livingio_API_Server+r.URL.RequestURI(),
		bytes.NewReader(audioBody))
	if err != nil {
		return nil, err
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

	client := &http.Client{
		Timeout: 15 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		},
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

func extractQueryText(body []byte) string {
	var resp struct {
		QueryResult struct {
			QueryText string `json:"queryText"`
		} `json:"queryResult"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return ""
	}
	return resp.QueryResult.QueryText
}

func queryN8N(text string) (*n8nResponse, error) {
	if conf.N8nWebhookURL == "" {
		return nil, fmt.Errorf("n8n webhook URL not configured")
	}
	payload := fmt.Sprintf(`{"event":"voice","text":"%s","language":"ru"}`,
		strings.ReplaceAll(strings.ReplaceAll(text, `\`, `\\`), `"`, `\"`))
	req, err := http.NewRequest("POST", conf.N8nWebhookURL,
		bytes.NewBufferString(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	log.Printf("n8n response: %s", string(body))
	var result n8nResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, err
	}
	return &result, nil
}

func getLivingAITTSURL(text, secret, auth string) string {
	encoded := strings.ReplaceAll(text, " ", "%20")
	// URL-encode Cyrillic
	var buf bytes.Buffer
	for _, r := range text {
		if r <= 127 && r != ' ' {
			buf.WriteRune(r)
		} else if r == ' ' {
			buf.WriteString("%20")
		} else {
			b := []byte(string(r))
			for _, c := range b {
				fmt.Fprintf(&buf, "%%%02X", c)
			}
		}
	}
	encoded = buf.String()

	url := fmt.Sprintf("https://%s/emo/speech/tts?l=ru&q=%s",
		conf.Livingio_API_Server, encoded)
	req, _ := http.NewRequest("GET", url, nil)
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	if secret != "" {
		req.Header.Set("Secret", secret)
	}
	req.Header.Del("User-Agent")
	client := &http.Client{
		Timeout: 8 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		},
	}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("living.ai TTS error: %v", err)
		return ""
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var ttsResp struct {
		Code int    `json:"code"`
		URL  string `json:"url"`
	}
	if err := json.Unmarshal(body, &ttsResp); err == nil && ttsResp.Code == 200 && ttsResp.URL != "" {
		log.Printf("living.ai TTS URL: %s", ttsResp.URL)
		return ttsResp.URL
	}
	log.Printf("living.ai TTS failed: %s", string(body))
	return ""
}

func getLocalTTSURL(text string) string {
	// Call emo-ai RHVoice TTS and get local audio URL
	payload := fmt.Sprintf(`{"text":"%s"}`,
		strings.ReplaceAll(strings.ReplaceAll(text, `\`, `\\`), `"`, `\"`))
	req, _ := http.NewRequest("POST", conf.ChatGptSpeakServer+"/tts",
		bytes.NewBufferString(payload))
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("local TTS error: %v", err)
		return ""
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var ttsResp struct {
		URL string `json:"url"`
	}
	if err := json.Unmarshal(body, &ttsResp); err == nil && ttsResp.URL != "" {
		return ttsResp.URL
	}
	return ""
}


func patchLivingAIResponse(livingBody []byte, newText, newTTSURL string) []byte {
	// Extract queryId and resultCode from living.ai response to reuse
	var resp struct {
		QueryId      string `json:"queryId"`
		LanguageCode string `json:"languageCode"`
		Index        int    `json:"index"`
		QueryResult  struct {
			ResultCode string `json:"resultCode"`
		} `json:"queryResult"`
	}
	if err := json.Unmarshal(livingBody, &resp); err != nil {
		return nil
	}
	qid := resp.QueryId
	rid := resp.QueryResult.ResultCode
	if qid == "" || rid == "" {
		return nil
	}
	// Clean text for EMO (remove quotes and special chars that break firmware)
	escaped := strings.ReplaceAll(newText, `\`, "")
	escaped = strings.ReplaceAll(escaped, `"`, "")
	escaped = strings.ReplaceAll(escaped, "\n", " ")
	escaped = strings.ReplaceAll(escaped, "\r", "")
	escaped = strings.ReplaceAll(escaped, "\t", " ")
	// Build response with exact living.ai field order
	j := `{"queryId":"` + qid +
		`","queryResult":{"rec_behavior":"speak","behavior_paras":{"txt":"` + escaped +
		`","url":"` + newTTSURL +
		`","pre_animation":"","post_animation":"","post_behavior":"","sentiment":"","listen":0},"resultCode":"` + rid +
		`","queryText":"` + escaped +
		`","intent":{"name":"chatgpt_speak","confidence":1}},"languageCode":"` + resp.LanguageCode +
		`","index":` + strconv.Itoa(resp.Index) + `}`
	return []byte(j)
}


func callEmoAIBuildAction(action, queryText, lang, idx string) []byte {
	payload := fmt.Sprintf(`{"action":"%s","query_text":"%s","lang":"%s","idx":"%s"}`,
		strings.ReplaceAll(action, `"`, `"`),
		strings.ReplaceAll(queryText, `"`, `"`),
		lang, idx)
	req, err := http.NewRequest("POST", conf.ChatGptSpeakServer+"/build_action",
		bytes.NewBufferString(payload))
	if err != nil {
		return nil
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("emo-ai /build_action error: %v", err)
		return nil
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		log.Printf("emo-ai /build_action status %d: %s", resp.StatusCode, string(body))
		return nil
	}
	return body
}

func buildSpeakJSON(text, ttsURL, lang, idx string, animation map[string]string) []byte {
	idxInt := 0
	if v, err := strconv.Atoi(idx); err == nil {
		idxInt = v
	}
	preAnim := ""
	postAnim := ""
	if animation != nil {
		if v, ok := animation["pre"]; ok {
			preAnim = v
		}
		if v, ok := animation["post"]; ok {
			postAnim = v
		}
	}
	// Escape text for JSON
	escapedText := strings.ReplaceAll(text, `\`, `\\`)
	escapedText = strings.ReplaceAll(escapedText, `"`, `\"`)

	n := time.Now().UnixNano()
	qid := fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", n&0xFFFFFFFF, (n>>32)&0xFFFF, (n>>48)&0xFFFF, (n>>16)&0xFFFF, n&0xFFFFFFFFFFFF)
	rid := fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", (n>>8)&0xFFFFFFFF, (n>>40)&0xFFFF, (n>>24)&0xFFFF, (n>>56)&0xFFFF, (n>>4)&0xFFFFFFFFFFFF)

	// Build JSON manually to match living.ai field order exactly
	j := `{"queryId":"` + qid + `","queryResult":{"rec_behavior":"speak","behavior_paras":{"txt":"` + escapedText + `","url":"` + ttsURL + `","pre_animation":"` + preAnim + `","post_animation":"` + postAnim + `","post_behavior":"","sentiment":"","listen":0},"resultCode":"` + rid + `","queryText":"` + escapedText + `","intent":{"name":"chatgpt_speak","confidence":1}},"languageCode":"` + lang + `","index":` + strconv.Itoa(idxInt) + `}`
	return []byte(j)
}

func buildActionJSON(action, queryText, lang, idx string) []byte {
	idxInt := 0
	if v, err := strconv.Atoi(idx); err == nil {
		idxInt = v
	}

	qr := map[string]interface{}{
		"queryText": queryText,
		"intent":    map[string]interface{}{"name": action, "confidence": 1},
	}

	switch action {
	case "dance":
		qr["rec_behavior"] = "dance"
		qr["behavior_paras"] = []interface{}{}
	case "dance_lights":
		qr["intent"] = map[string]interface{}{"name": "dance_with_lights", "confidence": 1}
		qr["rec_behavior"] = "dance"
		qr["behavior_paras"] = []interface{}{}
	case "be_quiet":
		qr["rec_behavior"] = "stay_still"
		qr["behavior_paras"] = map[string]interface{}{}
	case "sleep":
		qr["rec_behavior"] = "sleep"
		qr["behavior_paras"] = map[string]interface{}{}
	case "explore":
		qr["rec_behavior"] = "explore"
		qr["behavior_paras"] = map[string]interface{}{}
	case "listen_to_voice":
		qr["rec_behavior"] = "listen"
		qr["behavior_paras"] = map[string]interface{}{}
	default:
		qr["rec_behavior"] = action
		qr["behavior_paras"] = map[string]interface{}{}
	}

	resp := map[string]interface{}{
		"queryId":      fmt.Sprintf("%d", time.Now().UnixNano()),
		"queryResult":  qr,
		"languageCode": lang,
		"index":        idxInt,
	}
	b, _ := json.Marshal(resp)
	return b
}

func checkPendingSay() []byte {
	// Check emo-ai for pending_say/pending_action
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(conf.ChatGptSpeakServer + "/pending")
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var pending struct {
		Type   string            `json:"type"`
		Text   string            `json:"text"`
		URL    string            `json:"url"`
		Action string            `json:"action"`
	}
	if err := json.Unmarshal(body, &pending); err != nil || (pending.Text == "" && pending.Action == "") {
		return nil
	}
	log.Printf("pending found: %+v", pending)
	return body
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


// ── Pending response queue (for async operations like BLE photo) ────────────

var (
	pendingResponseMu   sync.Mutex
	pendingResponseText string

	lastLivingBodyMu sync.Mutex
	lastLivingBody   []byte
)

func queuePendingResponse(text string) {
	pendingResponseMu.Lock()
	pendingResponseText = text
	pendingResponseMu.Unlock()
	log.Printf("Queued pending response: %s", text)
}

func popPendingResponse() string {
	pendingResponseMu.Lock()
	defer pendingResponseMu.Unlock()
	r := pendingResponseText
	pendingResponseText = ""
	return r
}

func saveLastLivingBody(body []byte) {
	lastLivingBodyMu.Lock()
	lastLivingBody = make([]byte, len(body))
	copy(lastLivingBody, body)
	lastLivingBodyMu.Unlock()
}

func getLastLivingBody() []byte {
	lastLivingBodyMu.Lock()
	defer lastLivingBodyMu.Unlock()
	return lastLivingBody
}

func runAsyncAction(action string) {
	switch action {
	case "ble_photo_recognize":
		go asyncBLEPhotoRecognize()
	case "ble_photo_only":
		go asyncBLEPhotoOnly()
	default:
		log.Printf("Unknown async_action: %s", action)
	}
}

func asyncBLEPhotoRecognize() {
	log.Printf("Async: BLE photo + Coral recognize started")

	// 1. Take photo via BLE
	photoClient := &http.Client{Timeout: 25 * time.Second}
	photoResp, err := photoClient.Post("http://127.0.0.1:8091/photo", "", nil)
	if err != nil {
		log.Printf("Async: BLE photo error: %v", err)
		queuePendingResponse("Не удалось сделать фото.")
		return
	}
	defer photoResp.Body.Close()
	photoData, _ := io.ReadAll(photoResp.Body)

	if photoResp.StatusCode != 200 || len(photoData) < 100 {
		log.Printf("Async: BLE photo failed: status=%d size=%d", photoResp.StatusCode, len(photoData))
		queuePendingResponse("Фото не получилось.")
		return
	}
	log.Printf("Async: BLE photo received: %d bytes", len(photoData))

	// 2. Send to Coral Vision for detection
	coralReq, _ := http.NewRequest("POST",
		"http://127.0.0.1:8090/detect?lang=ru&threshold=0.3",
		bytes.NewReader(photoData))
	coralReq.Header.Set("Content-Type", "image/jpeg")
	coralClient := &http.Client{Timeout: 15 * time.Second}
	coralResp, err := coralClient.Do(coralReq)
	if err != nil {
		log.Printf("Async: Coral detect error: %v", err)
		queuePendingResponse("Не удалось распознать.")
		return
	}
	defer coralResp.Body.Close()
	coralBody, _ := io.ReadAll(coralResp.Body)
	log.Printf("Async: Coral result: %s", string(coralBody))

	// 3. Format result
	var result struct {
		Objects []struct {
			Label string  `json:"label"`
			Score float64 `json:"score"`
		} `json:"objects"`
	}
	if err := json.Unmarshal(coralBody, &result); err != nil {
		queuePendingResponse("Ошибка распознавания.")
		return
	}

	seen := map[string]bool{}
	var labels []string
	for _, obj := range result.Objects {
		if obj.Score > 0.4 && !seen[obj.Label] {
			seen[obj.Label] = true
			labels = append(labels, obj.Label)
		}
	}

	var text string
	switch len(labels) {
	case 0:
		text = "Не вижу ничего знакомого."
	case 1:
		text = "Я вижу " + labels[0] + "!"
	default:
		text = "Я вижу: " + strings.Join(labels[:len(labels)-1], ", ") + " и " + labels[len(labels)-1] + "!"
	}
	queuePendingResponse(text)
}

func asyncBLEPhotoOnly() {
	log.Printf("Async: BLE photo only")
	photoClient := &http.Client{Timeout: 25 * time.Second}
	resp, err := photoClient.Post("http://127.0.0.1:8091/photo", "", nil)
	if err != nil {
		log.Printf("Async: BLE photo error: %v", err)
		return
	}
	defer resp.Body.Close()
	io.ReadAll(resp.Body)
	log.Printf("Async: BLE photo done (status=%d)", resp.StatusCode)
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


	// detectintent — living.ai STT → n8n brain → response
	http.HandleFunc("/emo/voice/detectintent", func(w http.ResponseWriter, r *http.Request) {
		logRequest(r)
		saveLastCreds(r)

		w.Header().Set("Content-Type", "application/json; charset=utf-8")

		if r.Method != "POST" {
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, makeApiRequest(r))
			return
		}

		// Check for pending async response first
		if pendingText := popPendingResponse(); pendingText != "" {
			log.Printf("Pending response found: %s", pendingText)
			// Still send audio to living.ai to get a fresh response template
			audioBody, _ := io.ReadAll(r.Body)
			freshBody, err := sendToLivingAISync(r, audioBody)
			if err != nil {
				log.Printf("living.ai error for pending: %v", err)
			}
			secret := r.Header.Get("Secret")
			auth := r.Header.Get("Authorization")
			// Get TTS for pending text
			ttsURL := getLivingAITTSURL(pendingText, secret, auth)
			if ttsURL == "" {
				ttsURL = getLocalTTSURL(pendingText)
			}
			// Patch fresh living.ai response with our pending text
			var responseBody []byte
			if freshBody != nil {
				responseBody = patchLivingAIResponse(freshBody, pendingText, ttsURL)
			}
			if responseBody == nil {
				pLang := r.URL.Query().Get("languagecode")
				if pLang == "" { pLang = "ru" }
				pIdx := r.URL.Query().Get("index")
				responseBody = buildSpeakJSON(pendingText, ttsURL, pLang, pIdx, nil)
			}
			log.Printf("FINAL pending to EMO: %s", string(responseBody))
			w.WriteHeader(http.StatusOK)
			w.Write(responseBody)
			return
		}

		audioBody, _ := io.ReadAll(r.Body)
		logBody(r.Header.Get("Content-Type"), audioBody, "apiReq_")

		lang := r.URL.Query().Get("languagecode")
		if lang == "" {
			lang = "ru"
		}
		idx := r.URL.Query().Get("index")
		secret := r.Header.Get("Secret")
		auth := r.Header.Get("Authorization")

		// 1. Send audio to living.ai for STT (synchronous)
		livingBody, err := sendToLivingAISync(r, audioBody)
		if err != nil {
			log.Printf("living.ai STT error: %v", err)
			// Fallback: return empty response
			w.WriteHeader(http.StatusOK)
			w.Write(buildActionJSON("out_of_scope", "", lang, idx))
			return
		}
		log.Printf("living.ai STT: %s", string(livingBody))
		saveLastLivingBody(livingBody)

		if conf.EnableDatabaseAndAPI {
			saveRequest("LIVINGAI:"+r.URL.RequestURI(), "", string(livingBody))
		}

		// 2. Extract queryText from living.ai response
		queryText := extractQueryText(livingBody)
		if queryText == "" || queryText == "I cannot understand" {
			log.Printf("living.ai: no useful text (%q), returning their response as-is", queryText)
			w.WriteHeader(http.StatusOK)
			w.Write(livingBody)
			return
		}
		log.Printf("STT text: %q", queryText)

		// 3. Send text to n8n (the brain)
		n8nStart := time.Now()
		n8nResult, err := queryN8N(queryText)
		if err != nil {
			log.Printf("n8n error: %v, falling back to living.ai response", err)
			w.WriteHeader(http.StatusOK)
			w.Write(livingBody)
			return
		}

		// 4. Build response based on n8n result
		if n8nResult.Action != "" {
			log.Printf("n8n action: %s (query: %s)", n8nResult.Action, queryText)
			// Call emo-ai to build proper action response (it has full action mapping)
			actionResp := callEmoAIBuildAction(n8nResult.Action, queryText, lang, idx)
			if actionResp != nil {
				log.Printf("FINAL action response to EMO: %s", string(actionResp))
				w.WriteHeader(http.StatusOK)
				w.Write(actionResp)
				return
			}
			// Fallback: use local builder
			w.WriteHeader(http.StatusOK)
			w.Write(buildActionJSON(n8nResult.Action, queryText, lang, idx))
			return
		}

		if n8nResult.Text != "" {
			n8nDuration := time.Since(n8nStart)
			log.Printf("n8n text: %s (query: %s, took: %v)", n8nResult.Text, queryText, n8nDuration)
			// Check for async action — kick off background task
			if n8nResult.AsyncAction != "" {
				log.Printf("n8n async_action: %s", n8nResult.AsyncAction)
				runAsyncAction(n8nResult.AsyncAction)
			}

			if n8nDuration >= 2*time.Second {
				// GPT path — slow response. Use living.ai original response
				// (they already did ChatGPT + TTS, no timeout risk)
				log.Printf("GPT path (took %v) — using living.ai response", n8nDuration)
				w.WriteHeader(http.StatusOK)
				w.Write(livingBody)
				return
			}

			// Trigger path — fast response. Use our text with living.ai TTS
			ttsURL := getLivingAITTSURL(n8nResult.Text, secret, auth)
			if ttsURL == "" {
				ttsURL = getLocalTTSURL(n8nResult.Text)
			}
			if ttsURL == "" {
				log.Printf("TTS failed for: %s", n8nResult.Text)
			}
			patched := patchLivingAIResponse(livingBody, n8nResult.Text, ttsURL)
			if patched == nil {
				patched = buildSpeakJSON(n8nResult.Text, ttsURL, lang, idx, n8nResult.Animation)
			}
			log.Printf("FINAL speak response to EMO: %s", string(patched))
			w.WriteHeader(http.StatusOK)
			w.Write(patched)
			return
		}

		// 5. n8n returned nothing useful — use living.ai response as fallback
		log.Printf("n8n: no action/text, using living.ai response")
		w.WriteHeader(http.StatusOK)
		w.Write(livingBody)
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
		// Try local file first
		parts := strings.Split(r.URL.Path, "/tts/dl/")
		if len(parts) == 2 {
			audioID := parts[1]
			localPath := "/home/homer/emo-audio/" + audioID + ".mp3"
			if data, err := os.ReadFile(localPath); err == nil && len(data) > 100 {
				log.Printf("TTS: serving local file %s (%d bytes)", localPath, len(data))
				w.Header().Set("Content-Type", "audio/mpeg")
				w.Header().Set("Content-Length", strconv.Itoa(len(data)))
				w.WriteHeader(http.StatusOK)
				w.Write(data)
				return
			}
		}
		// Proxy to living.ai - use Host header to determine correct server
		host := r.Host
		if host == "" || !strings.Contains(host, "living.ai") {
			host = conf.Livingio_TTS_Server
		}
		ttsURL := "http://" + host + r.URL.RequestURI()
		log.Printf("TTS proxy: %s", ttsURL)
		req, _ := http.NewRequest("GET", ttsURL, nil)
		if v := r.Header.Get("Authorization"); v != "" {
			req.Header.Set("Authorization", v)
		}
		if v := r.Header.Get("Secret"); v != "" {
			req.Header.Set("Secret", v)
		}
		req.Header.Del("User-Agent")
		client := &http.Client{Timeout: 15 * time.Second}
		resp, err := client.Do(req)
		if err != nil {
			log.Printf("TTS proxy error: %v", err)
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		ct := resp.Header.Get("Content-Type")
		if ct == "" {
			ct = "audio/mpeg"
		}
		w.Header().Set("Content-Type", ct)
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		w.WriteHeader(resp.StatusCode)
		w.Write(body)
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
// CI trigger
