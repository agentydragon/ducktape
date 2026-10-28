package github

import (
	"fmt"
	"net/http"
)

func replyPage(w http.ResponseWriter, r *http.Request, path, query string, nextPage int, body string) {
	if nextPage != 0 {
		w.Header().Set("Link", fmt.Sprintf(`<http://%s%s?%s&page=%d>; rel="next"`, r.Host, path, query, nextPage))
	}
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, body)
}
