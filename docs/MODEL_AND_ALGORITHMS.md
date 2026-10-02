# Bài toán FFP và thuật toán SAT

## Mô tả bài toán

Đầu vào là đồ thị vô hướng đơn không trọng số $G=(V,E)$, tập đỉnh cháy ban đầu
không rỗng $B\subseteq V$, và số lính cứu hỏa $D\ge 1$ cho mỗi vòng. Mỗi đỉnh ở
một trong các trạng thái untouched, burned hoặc defended. Burned và defended là
trạng thái hấp thụ: một đỉnh không thể đổi sang trạng thái kia.

Ở đầu vòng $t\ge 1$, có thể bảo vệ tối đa $D$ đỉnh đang untouched. Sau đó lửa lan
đồng thời từ các đỉnh đã cháy trước vòng đó tới mọi láng giềng chưa được bảo vệ.
Containment xảy ra ở vòng đầu tiên không có đỉnh mới bị cháy.

Mục tiêu là tối thiểu hóa số đỉnh cháy cuối cùng:

$$
K=|B_{\mathrm{final}}|
$$

Điều này tương đương tối đa hóa số đỉnh được cứu $|V|-K$. Một schedule là dãy
$(P_1,\ldots,P_T)$, trong đó $P_t$ là các đỉnh được bảo vệ ở vòng $t$ và
$|P_t|\le D$.

## Biến SAT

Với mỗi đỉnh $v$ và thời điểm sau vòng $t$, encoding dùng:

| Biến | Nghĩa |
| --- | --- |
| $b_{v,t}$ | $v$ đã cháy sau vòng $t$ |
| $d_{v,t}$ | $v$ đã được bảo vệ trước hoặc trong vòng $t$ |
| $a_{v,t}$ | $v$ được bảo vệ lần đầu ở vòng $t$ |
| $h_T$ | activation literal yêu cầu containment tại horizon $T$ |

Ở layer 0 chỉ cần biến burned/defended. $a_{v,t}$ được tạo từ vòng 1 trở đi.
Variable manager cấp ID cho biến semantic, activation và auxiliary theo một
counter chung; `ITotalizer` nhận `top_id` hiện tại để không đụng ID.

## Công thức động học

Điều kiện đầu:

$$
\begin{aligned}
b_{v,0} &= 1 && \text{khi } v\in B,\\
b_{v,0} &= 0 && \text{khi } v\notin B,\\
d_{v,0} &= 0 && \text{với mọi } v.
\end{aligned}
$$

Burned và defended giữ nguyên trạng thái, và không thể cùng đúng:

$$
\begin{aligned}
b_{v,t-1} &\rightarrow b_{v,t},\\
d_{v,t-1} &\rightarrow d_{v,t},\\
\neg b_{v,t} &\lor \neg d_{v,t}.
\end{aligned}
$$

Hành động bảo vệ đúng khi đỉnh chuyển từ chưa bảo vệ sang đã bảo vệ:

$$
a_{v,t}\leftrightarrow\left(d_{v,t}\land\neg d_{v,t-1}\right)
$$

Mệnh đề CNF tương ứng:

$$
\begin{aligned}
\neg a_{v,t}&\lor d_{v,t},\\
\neg a_{v,t}&\lor\neg d_{v,t-1},\\
\neg d_{v,t}&\lor d_{v,t-1}\lor a_{v,t}.
\end{aligned}
$$

Mỗi firefighter chỉ bảo vệ một đỉnh trong một vòng:

$$
\sum_{v\in V}a_{v,t}\le D
$$

Ràng buộc này được mã hóa bằng một `ITotalizer` cho mỗi layer và được thêm cố
định vào solver.

Với mỗi cạnh vô hướng $\{u,v\}$, spread được mã hóa theo cả hai hướng. Ví dụ hướng
$u\rightarrow v$:

$$
b_{u,t-1}\land\neg d_{v,t}\rightarrow b_{v,t}
$$

hay trong CNF:

$$
\neg b_{u,t-1}\lor d_{v,t}\lor b_{v,t}
$$

Để cấm cháy tự phát, một đỉnh cháy ở vòng $t$ phải đã cháy từ trước hoặc có ít nhất
một láng giềng cháy ở vòng trước:

$$
b_{v,t}\rightarrow\left(b_{v,t-1}\lor\bigvee_{u\in N(v)}b_{u,t-1}\right)
$$

CNF:

$$
\neg b_{v,t}\lor b_{v,t-1}\lor\bigvee_{u\in N(v)}b_{u,t-1}
$$

Clause spread bắt cháy khi điều kiện lan đúng; clause này cấm encoding tự gán
$b_{v,t}$ để làm sai động học. Hai chiều cùng nhau mô tả chính xác quá trình cháy.

## Horizon, containment và objective

Containment ở horizon $T$ nghĩa là vòng $T$ không tạo thêm đỉnh cháy. Với tính đơn
điệu của burned, điều kiện là:

$$
\forall v\in V:\quad b_{v,T}\rightarrow b_{v,T-1}
$$

$h_T$ kích hoạt điều kiện bằng clause:

$$
\neg h_T\lor\neg b_{v,T}\lor b_{v,T-1}
$$

Mỗi query SAT chỉ đưa $h_T$ đang xét vào assumptions; do đó containment ở một
horizon cũ không áp vào query sau.

Với containment đã được kích hoạt, số đỉnh cháy cuối là:

$$
\sum_{v\in V}b_{v,T}
$$

Objective được ràng buộc bằng totalizer incremental:

$$
\sum_{v\in V}b_{v,T}\le K
$$

Các clause totalizer được giữ lại; bound $K$ được truyền bằng assumption cho từng
query. Encoder chỉ tạo layer tới horizon cần dùng, và chỉ nối thêm layer khi $T$ tăng.

## Preprocessing: lower bound và horizon tối đa

Preprocessing chạy multi-source BFS từ mọi đỉnh trong $B$ để tính khoảng cách ngắn
nhất $\delta(v)$ tới từng đỉnh. Lửa không thể tới $v$ trước vòng này, nên encoding
thêm unit clause $\neg b_{v,t}$ khi $t<\delta(v)$. Đỉnh không tới được từ $B$ có
$\delta(v)=\infty$.

Sau BFS, preprocessing tính lower bound rẻ:

$$
L=|B|+\max\left(0,\left|N(B)\setminus B\right|-D\right)
$$

Ít nhất $|B|$ đỉnh cháy ban đầu; trong vòng 1, tối đa $D$ láng giềng đang bị đe dọa
có thể được bảo vệ. Bound này không dựa vào UNSAT tại horizon ngắn.

Horizon tối đa là:

$$
T_{\max}=\left\lceil\frac{|V|}{D}\right\rceil
$$

Nếu một vòng chưa contained thì có thể giả sử đã bảo vệ đủ $D$ đỉnh: nếu còn ít hơn
$D$ đỉnh untouched, bảo vệ hết chúng sẽ chặn toàn bộ các đỉnh có thể cháy tiếp.
Sau $T_{\max}$ vòng không thể tiếp tục bảo vệ $D$ đỉnh mỗi vòng vì
$D T_{\max}\ge |V|$, trong khi có ít nhất một đỉnh burned. Vì thế containment có
thể đạt chậm nhất tại $T_{\max}$; search tới horizon này bao quát nghiệm tối ưu
toàn cục.

## Heuristic tạo incumbent

Heuristic bắt đầu ngay sau khi đọc instance để parent nhận một nghiệm khả thi sớm.
Ở mỗi vòng $t$, tập threatened là:

$$
C_t=\left(\bigcup_{u\in B_t}N(u)\right)\setminus(B_t\cup D_t)
$$

Trong đó $B_t$ là tập burned và $F_t$ là tập defended trước pha phòng vệ của vòng.
`Threat` mô phỏng từng vòng, rồi bảo vệ tối đa $D$ đỉnh trong $C_t$ theo thứ tự ID
tăng dần trước khi cho lửa lan.

Trong ngân sách heuristic còn lại chạy portfolio:

1. `DegreeThreat` xếp threatened vertices theo degree giảm dần, hòa theo ID.
2. `RandomizedThreat` xếp candidates theo ID rồi lấy ngẫu nhiên $D$ đỉnh khi có
   hơn $D$ candidates; RNG dùng seed CLI. Khi có không quá $D$ candidates, bảo vệ tất cả.
3. Mỗi schedule được mô phỏng để tính $K$ và containment. Giữ incumbent theo $K$ nhỏ
   nhất, rồi containment sớm hơn và schedule theo thứ tự từ điển khi hòa.
4. Lưu Pareto frontier theo $(T,K)$, loại nghiệm bị nghiệm khác trội ở cả hai đại lượng.

Ngân sách mặc định là $\min(5\text{ giây},0.05\,\texttt{time\_limit})$ và vẫn nằm
trong deadline chung. RandomizedThreat là heuristic thực dụng, không có bảo đảm tối ưu.

## Tìm kiếm tối ưu tuần tự

Worker giữ một solver CaDiCaL và dùng STK theo horizon. Gọi $U$ là $K$ của incumbent,
$L$ là lower bound hiện tại, $T$ là horizon hiện tại; khởi tạo
$T=\min(T_{\mathrm{inc}},T_{\max})$, trong đó $T_{\mathrm{inc}}$ là containment time
của incumbent heuristic.

Ở mọi horizon, query tuần tự:

$$
F(T,U-1)=\mathrm{SAT}?
$$

- SAT: giải mã schedule và trạng thái burned từ model; số biến $b_{v,T}$ đúng cho
  biết $K$ của model. Cập nhật incumbent/UB bằng $K$ này và tiếp tục hỏi $U-1$ ở cùng $T$.
- UNSAT với $T<T_{\max}$: không kết luận gì về lower bound toàn cục, vì có thể tồn
  tại nghiệm tốt hơn cần containment muộn hơn. Tăng horizon thành
  $\min(2T,T_{\max})$ rồi tiếp tục.
- UNSAT với $T=T_{\max}$: không tồn tại nghiệm nào tốt hơn incumbent trên horizon
  đủ bao quát bài toán; đặt $L=U$ và kết luận OPTIMAL.
- Timeout: trả checkpoint mới nhất. UB và schedule là incumbent trước đó đã được
  mô hình SAT hoặc heuristic tạo ra; query đang chạy không bị xem là UNSAT.

Sau khi worker dừng hoặc bị dừng, parent chạy trusted simulator một lần trên
schedule cuối. Kết quả $K$ và containment time được đối chiếu/cập nhật trước khi trả
JSON/CSV; pha kiểm tra này có `final_validation_time` riêng và không tính vào
`elapsed_total`.
