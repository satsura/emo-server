package main

import (
	"database/sql"
	"log"

	_ "modernc.org/sqlite"
)

var DB *sql.DB // Capitalized to be visible (though not strictly necessary if in same package)

func InitDB(path string) error {
	_db, err := sql.Open("sqlite", path)
	if err != nil {
		return err
	}
	DB = _db // Assign to global DB variable
	// Create a simple table for intercepted data
	query := `
	    CREATE TABLE IF NOT EXISTS requests (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
		endpoint TEXT,
		payload TEXT,
		response TEXT
	    );`

	_, err = DB.Exec(query)
	return err
}

func saveRequest(requestEndPoint string, payload string, response string) {
	log.Println("Saving request to DB...")
	_, err := DB.Exec("INSERT INTO requests (endpoint, payload, response) VALUES (?, ?, ?)", requestEndPoint, payload, response)
	if err != nil {
		log.Println("Failed to save to DB: ", err)
	}

	// TODO: implement a cleanup mechanism to limit DB size
}

func getAllRequests() ([]map[string]interface{}, error) {
	return getRequests(100, 0, "")
}

func getRequests(limit, offset int, filter string) ([]map[string]interface{}, error) {
	var rows *sql.Rows
	var err error
	if filter != "" {
		rows, err = DB.Query(
			"SELECT id, timestamp, endpoint, payload, response FROM requests WHERE endpoint LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?",
			"%"+filter+"%", limit, offset)
	} else {
		rows, err = DB.Query(
			"SELECT id, timestamp, endpoint, payload, response FROM requests ORDER BY id DESC LIMIT ? OFFSET ?",
			limit, offset)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var results []map[string]interface{}
	for rows.Next() {
		var id int
		var timestamp, endpoint, payload, response string
		if err := rows.Scan(&id, &timestamp, &endpoint, &payload, &response); err != nil {
			return nil, err
		}
		results = append(results, map[string]interface{}{
			"id":        id,
			"timestamp": timestamp,
			"endpoint":  endpoint,
			"payload":   payload,
			"response":  response,
		})
	}
	return results, rows.Err()
}

func getRequestCount(filter string) int {
	var count int
	if filter != "" {
		DB.QueryRow("SELECT COUNT(*) FROM requests WHERE endpoint LIKE ?", "%"+filter+"%").Scan(&count)
	} else {
		DB.QueryRow("SELECT COUNT(*) FROM requests").Scan(&count)
	}
	return count
}
