package main

import "encoding/json"

type Intent struct {
	Name       string  `json:"name,omitempty"`
	Confidence float64 `json:"confidence,omitempty"`
}

type BehaviorParas struct {
	UtilityType   string         `json:"utility_type,omitempty"`
	Time          []string       `json:"time,omitempty"`
	Txt           string         `json:"txt,omitempty"`
	Url           string         `json:"url,omitempty"`
	PreAnimation  string         `json:"pre_animation"`
	PostAnimation string         `json:"post_animation"`
	PostBehavior  string         `json:"post_behavior"`
	RecBehavior   string         `json:"rec_behavior,omitempty"`
	BehaviorParas *BehaviorParas `json:"behavior_paras,omitempty"`
	Sentiment     string         `json:"sentiment"`
	Listen        int            `json:"listen"`
	AnimationName string         `json:"animation_name,omitempty"`
	GameName      string         `json:"game_name,omitempty"`
	Type          string         `json:"type,omitempty"`
	Action        string         `json:"action,omitempty"`
}

type QueryResult struct {
	ResultCode    string          `json:"resultCode,omitempty"`
	QueryText     string          `json:"queryText,omitempty"`
	Intent        *Intent         `json:"intent,omitempty"`
	RecBehavior   string          `json:"rec_behavior,omitempty"`
	BehaviorParas json.RawMessage `json:"behavior_paras,omitempty"`
}

type QueryResponse struct {
	QueryId      string       `json:"queryId,omitempty"`
	QueryResult  *QueryResult `json:"queryResult,omitempty"`
	LanguageCode string       `json:"languageCode,omitempty"`
	Index        int          `json:"index,omitempty"`
}

type TokenResponse struct {
	AccessToken string `json:"access_token,omitempty"`
	ExpireIn    int    `json:"expire_in,omitempty"`
	Type        string `json:"type,omitempty"`
}

type EmoSpeechResponse struct {
	Code       int64  `json:"code"`
	Errmessage string `json:"errmessage"`
	Url        string `json:"url"`
}

type Trigger struct {
	Phrase string `json:"phrase"`
	Action string `json:"action"`
}
