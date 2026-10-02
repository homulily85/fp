# Đọc kết quả thực nghiệm

Batch runner tạo một file `summary.csv` và một JSON cho mỗi cặp instance và số
lính cứu hỏa. Một dòng CSV tương ứng với một lần chạy; JSON tương ứng với dòng
đó và chứa thêm schedule, metadata, cấu hình cùng thống kê chi tiết.

## Trạng thái nghiệm và cận

| Trường | Ý nghĩa |
| --- | --- |
| `status` | `OPTIMAL` khi đã chứng minh tối ưu; `FEASIBLE` khi có incumbent nhưng dừng do timeout; `ERROR` khi worker hoặc kiểm tra cuối gặp lỗi, hoặc hết giờ trước incumbent. |
| `termination` | Lý do kết thúc: thường là `PROVEN`, `TIME_LIMIT`, `TIME_LIMIT_NO_INCUMBENT`, `WORKER_ERROR`, `WORKER_EXIT` hoặc `FINAL_VALIDATION_ERROR`. |
| `initial_horizon_factor` | Hệ số $c$ dùng để khởi tạo $T_0$ từ containment time heuristic; mặc định $1.5$. |
| `horizon_growth_factor` | Hệ số $g$ tăng horizon sau UNSAT chưa đủ chứng nhận; mặc định 2. |
| `initial_t` | Horizon SAT khởi tạo sau heuristic, dùng ceiling và chặn tại certification horizon. |
| `t_old_safe`, `t_max` | Cận cũ $\lceil n/D\rceil$ trong dữ liệu JSON/CSV; `t_max` là alias tương thích, không điều khiển search. Không in trên log tiến trình. |
| `t_struct` | Cận cấu trúc $\lceil(n-|B|)/(D+1)\rceil$. |
| `t_from_ub` | Cận incumbent $U-|B|$. |
| `t_from_lb` | Cận lower bound $\lfloor(n-L)/D\rfloor+1$. |
| `t_cert` | Minimum của ba cận mới; UNSAT tại $T\ge t_{\mathrm{cert}}$ đủ chứng nhận tối ưu. |
| `max_encoded_t` | Layer lớn nhất thực sự được encode trong dữ liệu JSON/CSV; có thể lớn hơn certification horizon sau khi UB giảm. Không in trên log tiến trình. |
| `best_k` | Số đỉnh cháy của incumbent tốt nhất, ký hiệu $K$. |
| `saved` | Số đỉnh được cứu: $n-K$. |
| `lower_bound` | Cận dưới $L$ cho số đỉnh cháy tối ưu. |
| `upper_bound` | Cận trên $U$, lấy từ incumbent đã biết. Sau kiểm tra cuối, $U$ phải bằng $K$ của schedule. |
| `gap_abs` | Khoảng cách tuyệt đối $U-L$. |
| `gap_rel` | Khoảng cách tương đối $(U-L)/U$; bằng 0 khi $U=0$. |
| `best_containment_time` | Simulator xác nhận trạng thái sau vòng này contained: không còn cạnh từ đỉnh burned tới đỉnh untouched. Có thể bằng 0. |
| `incumbent_horizon` | Horizon mà SAT yêu cầu incumbent contained; simulator cuối phải xác nhận containment không muộn hơn mốc này. |
| `containment_semantics` | Định nghĩa containment; hiện là `stable_state_after_round`. |
| `reason` | Mã lỗi chi tiết; validation không nhất quán dùng `MODEL_VALIDATION_FAILED`. |

`OPTIMAL` có nghĩa $L=U$. Với `FEASIBLE`, schedule vẫn là incumbent khả thi nếu
`final_validation` là `PASSED`; hai cận cho biết khoảng nghiệm tối ưu chưa được
thu hẹp. $\texttt{gap\_abs}=0$ cũng tương ứng với chứng minh tối ưu.

## Kiểm tra schedule

`schedule` là mảng các mảng ID đỉnh. Mảng ngoài theo thứ tự vòng: phần tử đầu là
vòng 1. ID bắt đầu từ 0. Ví dụ:

```json
"schedule": [[3, 7], [5]]
```

nghĩa là bảo vệ đỉnh 3 và 7 ở vòng 1, rồi đỉnh 5 ở vòng 2. JSON cuối đã được
chuẩn hóa đến containment; các lượt sau khi lửa dừng không có trong schedule.
`best_containment_time` có thể nhỏ hơn `incumbent_horizon`, vì SAT chỉ yêu cầu
containment không muộn hơn horizon query.

Sau khi worker dừng hoặc bị timeout, parent chạy simulator đúng một lần trên
schedule cuối. `final_validation` là `PASSED`, `FAILED` hoặc `SKIPPED_NO_SCHEDULE`;
`final_validation_time` đo thời gian đọc instance và chạy simulator. Nếu kiểm tra
không khớp $K$ hoặc $U$, hoặc containment xảy ra muộn hơn `incumbent_horizon`, status
đổi thành `ERROR`, termination thành `FINAL_VALIDATION_ERROR` và
`reason=MODEL_VALIDATION_FAILED`. Khi chưa có schedule, trường này là
`SKIPPED_NO_SCHEDULE` và thời gian bằng 0.

## CSV tóm tắt

CSV dùng một hàng cho mỗi lần chạy, các trường gồm:

| Nhóm | Cột |
| --- | --- |
| Định danh/cấu hình | `instance`, `firefighters`, `initial_horizon_factor`, `horizon_growth_factor` |
| Horizon | `initial_t`, `t_old_safe`, `t_struct`, `t_from_ub`, `t_from_lb`, `t_cert`, `max_encoded_t` |
| Kết quả | `status`, `termination`, `best_k`, `saved`, `lower_bound`, `upper_bound`, `gap_abs`, `gap_rel` |
| Thời gian | `elapsed_total`, `solve_time`, `final_validation_time`, `encoding_time`, `sat_time` |
| Kiểm tra cuối | `containment_semantics`, `incumbent_horizon`, `final_validation`, `reason`, `error` |
| SAT | `sat_calls`, `sat_results`, `unsat_results` |
| Kích thước encoding | `n_semantic_vars`, `n_aux_vars`, `n_clauses` |

CSV được thiết kế để lọc, sắp xếp và tổng hợp hàng loạt. Nó không chứa schedule,
metadata đầy đủ, cấu hình hay mọi thống kê; mở JSON cùng tên để xem các trường đó.

## Thời gian và bộ đếm

- `elapsed_total`, `solve_time` và `total_time` đo bởi tiến trình parent đến khi worker dừng,
  gồm đọc instance, preprocessing, heuristic, sinh encoding, SAT, giao tiếp và
  thu hồi worker. Kiểm tra simulator cuối chạy sau mốc này.
- `final_validation_time` không cộng vào `elapsed_total`; nó bao gồm đọc lại input
  và mô phỏng schedule cuối.
- `read_time`, `preprocess_time`, `heuristic_time`, `encoding_time` và `sat_time`
  là bộ đếm pha trong worker. `sat_time` chỉ tính lời gọi `solve()`, không gồm
  giải mã model hay kiểm tra cuối.
- `sat_calls` tăng khi một query bắt đầu. `sat_results` và `unsat_results` chỉ đếm
  query đã kết thúc. Query bị dừng vì timeout không bị tính là UNSAT.
- `n_semantic_vars` đếm biến trạng thái/action, `n_activation_vars` đếm activation
  của containment, `n_aux_vars` đếm biến totalizer; `n_clauses` là số clause đã
  thêm vào solver.
- `number_of_horizon_extensions` đếm số lần encoding được nối thêm layer;
  `number_of_incumbent_improvements` đếm các lần giảm $K$.
- `snapshot_elapsed` là thời điểm worker tạo checkpoint. Khi timeout đang ở giữa
  bước encoding hoặc SAT, thống kê pha có thể chưa chứa công việc chưa checkpoint.

Trong JSON, `metadata` có seed và mô tả nguồn cháy; `config` có time limit, solver,
seed, ngân sách heuristic và phiên bản thư viện. `pareto_frontier` có thể xuất hiện
khi portfolio heuristic đã chạy; mỗi phần tử có containment time, $K$ và schedule.
Trường không áp dụng hoặc không có do worker dừng sớm có thể vắng mặt trong JSON;
CSV để trống các trường thiếu.

## Đọc log tiến trình

Mỗi log có timestamp UTC, tên instance và nhãn loại sự kiện. Chương trình chỉ in
khi checkpoint đổi trạng thái, không in heartbeat theo chu kỳ.

| Nhãn | Ý nghĩa |
| --- | --- |
| `[HEURISTIC]` | Heuristic tạo hoặc cải thiện incumbent. `UB` là số đỉnh cháy của nghiệm đó. |
| `[PREPROCESS]` | Preprocessing cập nhật lower bound $L$. |
| `[HORIZON_BOUND]` | In các cận horizon sau preprocessing/heuristic hoặc khi $L,U$ đổi. |
| `[SAT_QUERY]` | Query $F(T,U-1)$ đã được dựng và sắp giải. `SAT calls` đã tăng, nhưng kết quả query chưa được tính. |
| `[SAT]` | Query trước đó trả SAT và tìm incumbent mới; `UB` giảm. |
| `[UNSAT]` | Query trả UNSAT; xem `certifying` để biết nó đã chứng minh tối ưu chưa. |

Ví dụ:

```text
[... ] case.in [HORIZON_BOUND]: LB=5 UB=42 structural=25 from_ub=41 from_lb=46 cert=25
[... ] case.in [SAT_QUERY]: LB=5 UB=42 query_T=8 query=K<=41 SAT calls=1 (SAT=0, UNSAT=0)
[... ] case.in [UNSAT]: LB=5 UB=42 query_T=8 query=K<=41 next_T=16 T_cert=25 certifying=false status=FEASIBLE SAT calls=1 (SAT=0, UNSAT=1)
```

Ở dòng cận, certification là minimum của `structural`, `from_ub` và `from_lb`:
$\min(25,41,46)=25$. Query bắt đầu tại $T=8$ để hỏi có schedule contained
không muộn hơn vòng 8 với tối đa 41 đỉnh cháy hay không. UNSAT tại vòng 8 chưa đủ
chứng nhận tối ưu, nên $L$ giữ nguyên và bước tiếp theo là vòng 16. Nếu UNSAT tại
$T\ge T_{\mathrm{cert}}$, `certifying=true`; khi đó query đã loại mọi nghiệm tốt
hơn incumbent và solver kết luận tối ưu.

`query_T` là horizon của SAT call gần nhất. `next_T` chỉ xuất hiện khi UNSAT chưa
chứng nhận và thuật toán mở rộng horizon. Log không in horizon đang chọn trước query,
cận cũ `old`, hoặc layer lớn nhất đã encode; các trường này vẫn có trong JSON/CSV.

`SAT calls` đếm query đã bắt đầu. `SAT`/`UNSAT` trong ngoặc đếm query đã hoàn tất;
vì thế ngay tại `[SAT_QUERY]`, tổng hai số này có thể thấp hơn số calls. Query đang
chạy khi hết giờ không được tính là UNSAT. `elapsed` là thời gian wall-clock từ lúc
parent bắt đầu, không gồm final validation.

`old` là cận chẩn đoán cũ $\lceil n/D\rceil$. `structural`, `from_ub` và `from_lb`
lần lượt là ba cận certification; `cert` là minimum hiện tại. Các cận này được
tính lại sau khi $L$ hoặc $U$ đổi. Riêng trong dòng `[UNSAT]`, `T_cert` là giá trị
đã dùng để đánh giá query đó; JSON giữ nó trong `query_t_cert`. JSON `t_cert` là
giá trị hiện hành và có thể nhỏ hơn sau khi cập nhật cận.

Nếu tiến trình kết thúc với `status=FEASIBLE`, `best_k=upper_bound` vẫn đến từ
schedule incumbent. Final validation kiểm tra schedule sau khi search kết thúc;
chỉ khi `final_validation=PASSED` thì schedule được xác nhận khả thi.

## Ví dụ đọc một kết quả

Nếu một hàng có `status=FEASIBLE`, `best_k=42`, `saved=8`, `lower_bound=38` và
`upper_bound=42`, incumbent cứu 8 đỉnh, còn optimum nằm trong khoảng $[38,42]$ đỉnh
bị cháy. $U-L=4$; chưa có chứng minh optimum. Nếu `final_validation=PASSED`,
schedule của incumbent đã được simulator kiểm tra sau khi search kết thúc.

Nếu `status=OPTIMAL`, `best_k=42`, `lower_bound=42` và `upper_bound=42`, solver đã
chứng minh $K=42$ tối ưu; simulator xác nhận schedule cuối trước khi kết quả được trả.
