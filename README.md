# FFP STK-SAT

Solver SAT incremental cho **The Firefighter Problem**, tối thiểu hóa số đỉnh cháy
K, tương đương tối đa hóa số đỉnh được cứu `n - K`. Mọi nghiệm được kiểm chứng
bằng simulator độc lập trước khi nhận làm incumbent.

## Chạy

Trong môi trường đã có `python-sat`, `networkx`, `ruff`:

```bash
python -m ffp_sat dataset/50_ep0.1_0_gilbert_1.in --firefighters 1 \
  --time-limit 600 --solver cadical300 --seed 0 --json-out results/example.json

python -m ffp_sat.batch dataset --firefighters '[1,2]' --time-limit 600 --seed 0
```

Batch nhận một file `.in` hoặc thư mục dataset; chạy tuần tự với từng giá trị
firefighter, xuất JSON từng instance/D/seed và `summary.csv`, tiếp tục khi một
instance lỗi. `--firefighters '[a,b]'` khai triển đoạn nguyên dương bao gồm cả hai
đầu; có thể kết hợp nhiều số hoặc đoạn, ví dụ `--firefighters 1 '[3,5]' 8`.
Mặc định kết quả được ghi vào `results/dataset_name_sat-ffp_yyyy-mm-dd-hh-mm-ss/`
theo giờ local: dùng tên thư mục đầu vào khi chạy cả dataset, hoặc tên file bỏ
đuôi `.in` khi chạy một instance. Dùng `--out-dir` để chỉ định thư mục khác.
Mặc định từ chối ghi đè;
`--overwrite` cho phép thay thế.

`--heuristic-budget` là số giây dành cho incumbent ban đầu và portfolio; mặc định
`min(5, 0.05 * time_limit)`. Một lượt heuristic đang chạy có thể hoàn tất sau budget
riêng, nhưng parent vẫn áp deadline chung. Seed điều khiển RandomizedThreat; không
cam kết cùng số SAT calls khi chạy với timeout trên các máy khác nhau.

stdout in summary. stderr ghi timestamp UTC khi bounds thay đổi và mỗi 30 giây. Exit code:
0 nếu có nghiệm hợp lệ, 1 nếu lỗi hoặc timeout chưa có nghiệm, 2 nếu CLI sai.
Thời gian đọc, preprocessing, heuristic, encoding và SAT đều nằm trong deadline;
spawn, terminate và truyền kết quả qua Pipe được tính trong elapsed tổng.
Việc ghi JSON và in summary diễn ra sau khoảng thời gian được đo này. Sau deadline
parent có thể cần thêm khoảng 0,4 giây để terminate/kill và thu hồi process, cộng
chi phí hệ điều hành và xuất JSON.

## Input và nghiệm

Các dòng không rỗng của `.in`:

```text
seed
n
m
B description
|B|
B members separated by spaces
u v
... exactly m edges
```

ID đỉnh từ 0 tới n-1. V1 nhận đồ thị vô hướng đơn: từ chối self-loop, cạnh trùng,
B trùng, ID ngoài khoảng và số dòng cạnh sai. Description và tên file chỉ là
metadata; danh sách thành viên B xác định nguồn cháy.

Schedule JSON là danh sách các vòng; `schedule[0]` là vòng 1. Đầu mỗi vòng bảo vệ
tối đa D đỉnh untouched, sau đó lửa lan **đồng thời** từ các đỉnh cháy ở vòng trước.
Simulator dừng ở vòng đầu không có đỉnh cháy mới, kể cả vòng 1. Schedule được
chuẩn hóa tới containment; hành động sau containment bị bỏ. Schedule ngắn được
mô phỏng tiếp với các vòng phòng vệ rỗng.

JSON chứa schedule, K, saved, LB/UB, gap, containment time, horizon, config,
metadata, phiên bản dependency và instrumentation. `gap_rel=(UB-LB)/UB`.

| Status | Termination | Ý nghĩa |
| --- | --- | --- |
| OPTIMAL | PROVEN | LB = UB, optimum được chứng minh |
| FEASIBLE | TIME_LIMIT | Có nghiệm đã kiểm chứng, chưa chứng minh tối ưu |
| ERROR | TIME_LIMIT_NO_INCUMBENT | Deadline trước checkpoint khả thi; UB/schedule/gap null |
| ERROR | WORKER_ERROR hoặc WORKER_EXIT | Lỗi; giữ incumbent trước lỗi nếu đã có |

`elapsed_total`/`total_time` đo bởi parent. `snapshot_elapsed` chỉ thời điểm checkpoint
thống kê. Nếu hard-kill trong encoding/SAT, thời gian pha đang chạy và biến/clauses
mới chưa gửi không có trong snapshot. `sat_calls` đếm call bắt đầu; `sat_results` và
`unsat_results` chỉ đếm call hoàn tất. Call bị kill không bao giờ được hiểu là UNSAT.

## Thiết kế và tính đúng

- `instance`, `simulator`, `preprocess`, `heuristic`: parse, trusted checker, BFS và incumbent.
- `variables`, `totalizer`, `encoder`: ID duy nhất, cardinality và clause theo layer.
- `stk_solver`, `worker`: STK search và một solver giữ xuyên suốt worker; parent giữ checkpoint qua Pipe.
- `result`, `cli`, `batch`: kết quả atomic và giao diện thực nghiệm.

Encoding có b[v,t] (cháy), d[v,t] (đã bảo vệ), a[v,t] (bảo vệ mới). Monotonicity,
loại trừ cháy–phòng vệ, spread và clause cấm cháy tự phát mô tả chính xác động học.
Khoảng cách BFS cấm cháy trước thời điểm lửa có thể tới. Mỗi layer có totalizer
firefighter với bound cố định. Objective totalizer được tạo lazily; bound objective
và containment activation được truyền bằng assumptions. Khi đổi horizon chỉ nối
layer, không rebuild solver hay giữ containment cũ bằng unit clause.

Lower bound ban đầu:

```text
|B| + max(0, |N(B) \ B| - D)
```

Horizon an toàn `T_max = ceil(n/D)`: bổ sung các lượt bảo vệ chưa dùng bằng đỉnh
untouched không thể làm nghiệm xấu hơn. Trong chiến lược được bổ sung này, nếu
một vòng chưa contained thì phải bảo vệ đủ D đỉnh (nếu có ít hơn D đỉnh untouched,
bảo vệ tất cả sẽ contained ngay). Nếu đến vòng T_max vẫn lan, các vòng đó đã bảo
vệ D*T_max đỉnh, ngoài ít nhất một đỉnh cháy ban đầu: vượt n, mâu thuẫn. Do đó có
nghiệm tối ưu contained không muộn hơn T_max.

Ở horizon ngắn, query `F(T,U-1)` để cải thiện incumbent; UNSAT chỉ tăng T, không
tăng LB. Tại T_max, binary search tăng LB bằng UNSAT hoặc giảm UB bằng nghiệm đã
kiểm chứng. Luôn giữ `LB <= K* <= UB`; chỉ trả OPTIMAL khi LB = UB.

V1 không loại bỏ thành phần rời rạc hoặc thêm symmetry breaking. Với D=1 và graph
lớn, horizon cao có thể tạo rất nhiều clause; timeout trả incumbent và bounds,
không bảo đảm chứng minh optimum trong budget.

## Kiểm thử

```bash
python -m unittest discover -v
python -m ruff check ffp_sat tests
python -m ruff format --check ffp_sat tests
```

Oracle dùng bitmask, liệt kê mọi tập phòng vệ tối đa D và memoize trạng thái;
không gọi simulator production. Bộ kiểm thử đối chiếu STK với tất cả simple graph
tới n=4 (B={0}), cùng 100 graph ngẫu nhiên n=5–8, D=1–3, B đơn hoặc nhiều nguồn.
Các query horizon/bound được kiểm tra riêng với oracle, bao gồm tính đơn điệu.
Có kiểm thử totalizer exhaustive, collision ID, activation, cháy tự phát,
UNSAT horizon ngắn, chỉ một solver, timeout/kill/crash, CLI và batch.

Không chạy toàn bộ dataset 600 giây/instance trong bộ kiểm thử mặc định.
