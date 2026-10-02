
Đang tải tin nhắn cũ hơn…

Recording 2026-09-23 111746.mp4
Tệp
tôi muốn làm 1 tool theo dõi near giống video này, ông xem lọc những cái cần thiết và làm bảng theo dõi near đi,  tìm api của free nhé

Đã xử lý trong 5m 54s




Được ông. Tôi đã nghiên cứu bộ API miễn phí hiện có của NEAR để làm theo hướng “NEAR Monitor”, nhưng có một điểm tôi phải nói rõ: file video ông gửi là MP4 nhưng công cụ hiện tại không trích xuất được hình/khung hình từ video, nên tôi chưa thể khẳng định chính xác từng widget trong video để copy 1:1. Tôi không muốn đoán nội dung video rồi làm sai.

Tuy vậy, tôi đã lọc được bộ dữ liệu miễn phí và đáng làm nhất cho một bảng NEAR riêng.

Bộ API tôi đề xuất
Dữ liệu	Nguồn	Miễn phí
Giá NEAR	DefiLlama	✅
TVL NEAR	DefiLlama	✅
Stablecoin trên NEAR	DefiLlama	✅
NEAR Intents TVL/volume/fees	DefiLlama	✅
Block / epoch / validator / staking	NEAR RPC	✅
Vị thế staking cá nhân	NEAR RPC + staking contract	✅
Validator APY/fee/delegator	NearBlocks	🟢 Free
Transactions / hoạt động mạng	NearBlocks	🟢 Free
Account / token / staking discovery	FastNEAR	🟢 Có endpoint public
Giá + market cap chi tiết	NearBlocks/CoinGecko tùy chọn	🟢 Free tier
NEAR có public RPC tại rpc.mainnet.near.org; RPC cho phép đọc trạng thái mạng, validator và gọi các hàm view của smart contract. 

NearBlocks Free hiện công bố 6 calls/phút, 333 calls/ngày và 10.000 calls/tháng, phù hợp để dùng làm lớp indexed data nếu mình cache hợp lý. 

FastNEAR có endpoint staking cho account như /v1/account/{account_id}/staking; tài liệu hiện tại cho thấy endpoint này có thể dùng để lấy các staking pool liên quan tới account. 

DefiLlama hiện có dữ liệu TVL, giá, stablecoin và nhiều metric DeFi; NEAR chain đang được theo dõi trực tiếp. 

Tôi sẽ không nhét quá nhiều thứ vào bảng
Tôi nghĩ NEAR Monitor nên tập trung vào 6 nhóm:

🟢 1. Thị trường NEAR
NEAR Price
$X.XX

24h: +X.XX%
7D: +X.XX%
30D: +X.XX%

Market Cap
FDV
24h Volume
🟦 2. Sức khỏe mạng
Transactions 24h
Active Addresses
New Addresses
TPS
Block Time
Active Validators
Total Validators
🟣 3. Staking
Đây là phần tôi nghĩ rất quan trọng với ông:

Total NEAR Staked
Staking Ratio
Current APY
Validator Fee

Pool:
ledgerbyfigment.poolv1.near

Pool Stake
Delegators
Pool APY
Stake change 24h
Với staking cá nhân, NEAR staking pool chuẩn có các hàm:

get_account_staked_balance
get_account_unstaked_balance
get_account_total_balance
get_total_staked_balance
nên mình có thể đọc trực tiếp từ smart contract, không cần API trả tiền. 

🟠 4. DeFi / hệ sinh thái
NEAR TVL
TVL 24h
TVL 7D
Stablecoin Supply

DEX Volume 24h
DEX Volume 7D

NEAR Intents:
TVL
Volume 24h
Fees 24h
Revenue 24h
NEAR Intents hiện có dữ liệu TVL, volume, fees và revenue trên DefiLlama, nên phần này rất đáng đưa vào dashboard. 

🔵 5. Dòng tiền / hoạt động
Sau này tôi muốn thêm:

NEAR Exchange Inflow
NEAR Exchange Outflow
Netflow

Top Wallets
Whale Transfers
Large Transactions
Nhưng phần này tôi sẽ không dùng API trả phí ngay.

Mình sẽ trước tiên làm bằng NearBlocks Free + danh sách ví sàn đã xác định, cache dữ liệu để không đốt quota. NearBlocks có API account, transaction và validator để xây lớp này. 

🧠 6. Cuối cùng là “NEAR Network Strength”
Tương tự dashboard BTC của ông, nhưng không dùng một điểm số tùy tiện ngay từ đầu.

Nó sẽ đọc:

Price
   +
Staking
   +
Active users
   +
Transactions
   +
TVL
   +
Stablecoins
   +
DEX volume
   +
NEAR Intents
   +
Whale / exchange flow
   +
Developer activity
rồi báo:

🟢 Network fundamentals đang cải thiện

hoặc

🟡 Tín hiệu hỗn hợp

hoặc

🔴 Network fundamentals đang suy yếu

Và bên dưới phải ghi rõ vì sao, ví dụ:

🟢 TVL 7D tăng
🟢 DEX volume tăng
🟢 Active addresses tăng
🔴 Stablecoin supply giảm
🟡 Giá NEAR giảm

Chứ không chỉ cho ông một con số kiểu 72/100.

Tôi sẽ làm kiến trúc như này
                    🟢 NEAR MONITOR
                           │
       ┌───────────────────┼───────────────────┐
       ↓                   ↓                   ↓
   MARKET             NETWORK             STAKING
       │                   │                   │
   Price              TPS                  APY
   Volume             Txns                 Pool
   MCap               Users                Delegators
       │                   │                   │
       └───────────────────┼───────────────────┘
                           ↓
                    DEFI / ECOSYSTEM
                           │
                 TVL / Stablecoin
                 DEX / Intents
                           │
                           ↓
                   FLOW / WHALES
                           │
                           ↓
                   🧠 NEAR STRENGTH
Và tôi muốn giữ nguyên nguyên tắc của dashboard BTC
Dữ liệu trước → phân tích sau.

Không để tool nhìn thấy:

Giá NEAR tăng 5%

rồi kết luận:

“Network đang khỏe.”

Nếu giá tăng nhưng:

TVL giảm
active users giảm
DEX volume giảm
staking giảm
thì dashboard phải báo tín hiệu trái chiều.

Một điểm rất hay
NEAR hiện có chính dashboard analytics của NEAR giới thiệu các nguồn như Artemis, Dune, Flipside và NearBlocks, trong đó NEAR mô tả Artemis dùng cho active users, fees, developer engagement và network fundamentals; NearBlocks dùng cho block, transaction, account và validator data. 

Nên bộ chỉ số trên không phải chọn ngẫu nhiên.

Tôi đề xuất bản đầu tiên chỉ làm những thứ lấy được miễn phí và đáng tin, sau đó mới thêm whale/exchange/developer nếu tìm được nguồn free đủ ổn.

Và vì video chưa đọc được hình nên tôi chưa muốn gửi ông một file V32 giả vờ là “đã làm giống video”. Nếu ông chụp cho tôi 2–3 màn hình chính trong video, tôi có thể lọc chính xác các widget cần thiết rồi dựng NEAR Monitor V1 theo đúng giao diện đó. 

Ông không xem được video tôi gửi à
Có ông — ông gửi video được và tôi đã nhận được file, nhưng ở lượt trước tôi nói chưa rõ.

Vấn đề là công cụ đọc video hiện tại không giải mã trực tiếp được nội dung hình ảnh của MP4, nên tôi không thể xem từng khung hình như khi ông gửi ảnh. Tôi đã thử đọc file nhưng hệ thống chỉ nhận diện được đây là video, không lấy được nội dung hình/âm thanh để phân tích.

Vì vậy câu trước tôi nói “không xem được video” là hơi cụt — không phải ông gửi sai hay tôi không nhận được video.

Nếu mục tiêu là làm tool NEAR giống video, cách tốt nhất là ông chụp khoảng 3–5 ảnh màn hình ở những đoạn quan trọng trong video gửi tôi. Tôi sẽ:

Nhận diện từng bảng/chỉ số trong video.
Lọc cái thực sự có giá trị cho NEAR.
Tìm API miễn phí cho từng chỉ số.
Loại bỏ những chỉ số không có nguồn free hoặc dữ liệu không đáng tin.
Thiết kế layout NEAR Monitor.
Sau đó tôi làm code và test trước khi gửi ông.
Nếu video có nhiều màn hình, ông cứ pause video → chụp màn hình, không cần chụp hết. Chỉ cần các màn hình chính là đủ.

Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
Tệp đính kèm của người dùng
tool như vầy, ông chắt lọc xem cái nào cần thiết và có lấy api miễn phí thì thiết kế cho tôi

Đã xử lý trong 1m 5s
Đúng rồi ông. Nhìn các ảnh này thì giờ tôi hiểu khá rõ tool ông muốn làm. Đây không phải chỉ là bảng giá NEAR, mà là kiểu NEAR On-chain Intelligence / Health Monitor giống A1 Radar: gom dữ liệu giá + holder + dòng tiền + cost basis + whale để đánh giá trạng thái của NEAR.

Tôi đã đối chiếu các module trong ảnh với các nguồn API hiện có. Kết quả là không nên copy toàn bộ, vì một số thứ như “Smart Bands” và cost-basis cohort của A1 Radar là dữ liệu/thuật toán riêng. Tôi đề xuất lọc như sau.

🟢 NEAR Monitor — bản tôi đề xuất
1. Market Snapshot — GIỮ
Đây là hàng đầu tiên:

Chỉ số	Dữ liệu
Giá NEAR	✅
Market Cap	✅
FDV	✅
Volume 24h	✅
Circulating Supply	✅
24h %	✅
7D %	✅
30D %	✅
Phần này lấy từ nguồn giá thị trường; tôi sẽ ưu tiên nguồn miễn phí và cache để không đốt quota.

2. NEAR Radar Score — GIỮ nhưng làm lại
Ảnh có:

BULL 69
ACCUMULATION 85
HIGH RISK 51
OVERHEATING 62
SUSTAINABLE 66

Tôi không muốn bê nguyên công thức của A1 Radar, vì mình không biết công thức nội bộ của họ.

Thay vào đó mình làm:

🧠 NEAR Radar
                 NEAR RADAR
                    │
       ┌────────────┼────────────┐
       ↓            ↓            ↓
    MOMENTUM    ACCUMULATION    RISK
       │            │            │
     Giá          Whale         Volatility
     Volume       Holder        Drawdown
     Trend        Flow          Leverage*
       │            │            │
       └────────────┼────────────┘
                    ↓
              NETWORK HEALTH
                    │
              TVL / Users /
          Transactions / Staking
Và bên cạnh điểm số phải có lý do.

Ví dụ:

🟢 Momentum       72/100
   Giá 7D tăng
   Volume tăng
   → động lượng cải thiện

🟢 Accumulation   68/100
   Top holder balance tăng
   Net holder flow dương
   → có dấu hiệu tích lũy

🟡 Risk           49/100
   Volatility tăng
   Price đang cách MA khá xa
   → rủi ro ngắn hạn trung tính

🟢 Network        74/100
   Active users tăng
   Transactions tăng
   TVL ổn định
Điểm quan trọng: đây là điểm do dashboard tự tính, không phải dữ liệu chính thức của NEAR.

3. Investor Positioning — GIỮ, nhưng chỉnh cách làm
Đây là phần rất hay trong ảnh:

<24H
1D–1W
1W–1M
1M–3M
3M–6M
6M–12M
1Y–2Y
2Y–3Y
3Y–5Y
5Y–7Y
và:

AVG COST BASIS

Nhưng có vấn đề
Để tính chính xác:

"những người mua NEAR 1–3 tháng trước có giá vốn trung bình bao nhiêu?"

phải có lịch sử transfer + balance của holder theo thời gian.

Không thể chỉ lấy giá NEAR hiện tại rồi suy ra.

Vì vậy tôi chia thành 2 tầng:
V1 miễn phí:

Holder cohorts
<24H
1D–7D
7D–30D
30D–90D
90D–180D
180D–1Y
1Y+
→ theo dõi số lượng NEAR/holder activity của từng nhóm, nếu dữ liệu đủ.

V2:

→ dựng cost basis chính xác hơn từ lịch sử transfer.

Tôi không muốn hiển thị một con số $1.9327 rồi gọi đó là cost basis nếu dữ liệu chưa đủ.

4. Money Flow Index — RẤT NÊN GIỮ
Đây là một trong những phần tôi đánh giá cao nhất trong ảnh.

Biểu đồ:

Money Flow
       │
       │       ╭──╮
       │   ╭───╯  ╰───╮
       │───╯           ╰────
       │
       └──────────────────────→ thời gian
                +
             NEAR Price
Nhưng tôi sẽ đổi tên thành:

💰 NEAR Capital Flow
Theo dõi:

Exchange inflow
Exchange outflow
Netflow
Whale flow
Large transfers
Price
Sau đó tạo:

Net Flow 24H
Net Flow 7D
Net Flow 30D
Quan trọng
Netflow âm không tự động có nghĩa "mua".

Ví dụ:

NEAR rời sàn → có thể là rút về ví cá nhân/staking/DeFi.

Do đó dashboard sẽ ghi:

🟢 Exchange outflow tăng
→ áp lực cung trên sàn giảm có thể tích cực
→ cần kết hợp price/holder flow để xác nhận.

5. Smart Bands — GIỮ NHƯNG KHÔNG COPY MÙ QUÁNG
Ảnh của ông có:

Realized Price
Balance Price
Delta Price
UnderValue Price
Diamond Price
EMA50
EMA200
Đây là phần rất đẹp để đưa vào chart.

Nhưng với NEAR, mình phải phân biệt:

Có thể làm ngay
NEAR Price
EMA20
EMA50
EMA200
Volume
ATH
ATL
52W High
52W Low
Còn:
Realized Price
Balance Price
Delta Price
Undervalue Price
Diamond Price
không thể lấy từ một API free đơn giản rồi coi như dữ liệu chuẩn.

Tôi sẽ chỉ thêm khi chúng ta có công thức/dữ liệu đủ để tính.

6. Holder Flow Index — CỰC KỲ NÊN CÓ
Đây là phần trong ảnh tôi muốn giữ gần như nguyên ý tưởng:

Holder Flow

Top 1–100
101–200
201–300
301–500
501–1000
Sau đó:

          NEAR
            │
      Holder Balance
            │
      ┌─────┼─────┐
      ↓     ↓     ↓
   Top100 101-200 201-300
      ↓     ↓     ↓
   +52k    +3.5k   -2.3k
Rồi vẽ:

Accumulation / Distribution

Ví dụ:

TOP 1–100      +52,908 NEAR 🟢
101–200         +3,583 NEAR 🟢
201–300         +2,306 NEAR 🟢
301–500         +6,716 NEAR 🟢
501–1000       +10,519 NEAR 🟢

NET HOLDER FLOW
+79,xxx NEAR
Cái này có thể làm được bằng dữ liệu indexed holder. FastNear hiện có public API cho các view tài khoản, staking và cả top holders của fungible token; nhiều endpoint public không cần API key. 

7. Clustering Wallets — GIỮ
Phần này trong ảnh:

Clustering Wallets

tôi cũng muốn đưa vào.

Nhưng không cần làm quá phức tạp ngay.

V1:
Theo dõi:

Whale accumulation
Whale distribution
Large transfer
Top holder balance change
Chia:

🟢 Accumulation
🔴 Distribution
⚪ Neutral
và timeframe:

1D | 7D | 30D
Ví dụ:

30D

Accumulation       +1.82M NEAR
Distribution       -1.17M NEAR
────────────────────────────
Net                +650K NEAR
Sau này
Mới làm:

Wallet A + Wallet B + Wallet C có liên hệ giao dịch → cluster

Phần này phức tạp hơn và dễ tạo false positive, nên không đưa vào bản đầu.

8. Network Health — đây là phần A1 Radar thiếu mà tôi muốn thêm
Đây sẽ là lợi thế của tool riêng của ông.

🌐 NEAR NETWORK HEALTH

Active Validators       XXX
Total Staked             XX.X%
Transactions 24H         X.XXM
Active Addresses         XXXK
New Addresses            XXK
TPS                      XX
TVL                      $XXM
Stablecoin Supply        $XXM
NEAR RPC/FastNear có thể lấy validator set, stake và trạng thái mạng trực tiếp; FastNear cũng có public RPC không cần API key cho các truy vấn canonical. 

9. NEAR DeFi — THÊM
Tôi muốn thêm một khung mà ảnh gốc không nhấn mạnh đủ:

🏦 NEAR ECOSYSTEM

TVL                  $XXM
TVL 7D               +X.X%
TVL 30D              +X.X%

Stablecoin Supply    $XXM
Stablecoin 7D        +X.X%

DEX Volume 24H       $XXM
DEX Volume 7D        $XXXM

NEAR Intents Volume  $XXM
Dune hiện vẫn có dữ liệu NEAR và có trang blockchain NEAR riêng; Dune xác nhận NEAR được hỗ trợ và có dữ liệu transactions, DeFi, cross-chain, v.v. 

Tôi sẽ bỏ những gì?
❌ Không làm ngay
1. "AI Radar Score" copy nguyên A1

→ không biết thuật toán bên trong.

2. Cost basis giả

→ thiếu dữ liệu thì không tự bịa.

3. Diamond Price / Delta Price

→ chỉ thêm khi xác định được công thức hợp lệ.

4. Clustering quá sâu

→ dễ biến dữ liệu giao dịch thành kết luận sai.

5. Leverage / derivatives

→ NEAR không có dữ liệu free thống nhất tốt như BTC/ETH.

Và tôi muốn bố trí dashboard như này
╔══════════════════════════════════════════════════════════════╗
║                    🟢 NEAR MONITOR                          ║
║             cập nhật dữ liệu tự động                        ║
╠══════════════════════════════════════════════════════════════╣
║ PRICE       MARKET CAP      VOLUME 24H       SUPPLY          ║
║ $X.XX       $X.XXB         $XXXM            XXXM             ║
╠══════════════════════════════════════════════════════════════╣
║ 🧠 NEAR RADAR                                                ║
║                                                              ║
║ Momentum       Accumulation       Risk       Network         ║
║   72/100          68/100          49/100      74/100        ║
║                                                              ║
║             🟢 Tín hiệu tổng hợp: TÍCH CỰC                  ║
╠══════════════════════════════╦═══════════════════════════════╣
║ 💰 CAPITAL FLOW              ║ 🐋 HOLDER FLOW                ║
║                              ║                               ║
║ Inflow       XXX NEAR        ║ Top 1-100       +XXK          ║
║ Outflow     -XXX NEAR        ║ 101-200         +XK           ║
║ Netflow      +XXK            ║ 201-300         -XK           ║
║                              ║ 301-500         +XK           ║
║ 1D | 7D | 30D                ║ 501-1000        +XK           ║
╠══════════════════════════════╩═══════════════════════════════╣
║ 📊 HOLDER / INVESTOR POSITIONING                             ║
║                                                              ║
║ <24H | 1D-7D | 7D-30D | 30D-90D | 90D-180D | 180D+         ║
║                                                              ║
║        Holder distribution / cost-basis proxy               ║
╠══════════════════════════════════════════════════════════════╣
║ 📈 SMART BANDS                                               ║
║                                                              ║
║        NEAR price + EMA20/50/200 + volume                   ║
║                                                              ║
╠══════════════════════════════╦═══════════════════════════════╣
║ 🌐 NETWORK HEALTH            ║ 🏦 NEAR ECOSYSTEM             ║
║ Validators                   ║ TVL                           ║
║ Staking %                    ║ Stablecoin                    ║
║ Transactions                 ║ DEX Volume                    ║
║ Active addresses             ║ NEAR Intents                  ║
╠══════════════════════════════╩═══════════════════════════════╣
║ 🧠 PHÂN TÍCH CUỐI                                             ║
║                                                              ║
║ Momentum:       🟢                                             ║
║ Holder Flow:    🟢                                             ║
║ Capital Flow:   🟡                                             ║
║ Network:        🟢                                             ║
║ Price structure:🟡                                             ║
║                                                              ║
║ → Tín hiệu đang đồng thuận / chưa đồng thuận                 ║
╚══════════════════════════════════════════════════════════════╝
Nguồn dữ liệu tôi sẽ ưu tiên
NEAR RPC / FastNear → network, validator, staking, account, holder. FastNear hiện có public endpoints và tài liệu ghi rõ các API account/staking thường hoạt động không cần API key. 

NearBlocks Free → indexed NEAR data bổ sung. Free hiện có giới hạn 6 calls/phút, 333 calls/ngày, 10.000 calls/tháng, nên tôi sẽ cache mạnh và không gọi liên tục. 

DefiLlama → TVL, stablecoin, giá và DeFi ecosystem.

Dune → dùng khi cần phân tích lịch sử NEAR sâu hơn. Dune có dữ liệu NEAR và hỗ trợ API; tuy nhiên API sử dụng credit theo dữ liệu truy vấn nên tôi sẽ không lấy Dune làm nguồn chính cho mọi lần refresh. 

Điểm tôi thích nhất ở thiết kế này
Nó sẽ không chỉ nói "NEAR tăng/giảm".

Ví dụ:

NEAR +8%
nhưng Top 100 holder giảm
exchange inflow tăng
TVL giảm
active address giảm

Dashboard sẽ không đơn giản kết luận:

🟢 NEAR bullish.

Mà sẽ ghi:

🟠 Giá tăng nhưng dữ liệu holder/network chưa xác nhận.

Ngược lại:

Giá +5%
Top holder +
Net outflow +
TVL +
Active addresses +
Volume +

→ các nhóm dữ liệu đang đồng thuận hơn.

Đây chính là phần tôi nghĩ nên làm khác A1 Radar: mọi điểm số đều truy ngược được về dữ liệu gốc, chứ không phải một "AI score" bí mật.

Tôi đề xuất bản đầu tiên lấy tên NEAR Monitor V1 và ưu tiên 7 module: Market → Radar → Capital Flow → Holder Flow → Investor Positioning → Network Health → Ecosystem. Sau đó mới nâng cấp Smart Bands/Clustering khi có dữ liệu đủ tốt.

ok giờ bắt đầu làm đi

app (1).py
Code
ok bắt đầu làm bảng near đi rồi gắng vào file này, test ok rồi gửi tôi

Đã xử lý trong 3m 21s

Ok ông, tôi đã bắt đầu làm NEAR Monitor và gắn thẳng vào file ông gửi. File gốc là app Streamlit hiện tại của ông, có sẵn Plotly/Requests/Pandas nên tôi giữ nguyên kiến trúc đó. 

NEAR Monitor V1 đã thêm
📌 Market Snapshot
NEAR Price
Market Cap
FDV
Volume 24h
Circulating Supply
NEAR TVL
🧠 NEAR Radar
Momentum
Capital Flow
Network
Ecosystem
Risk
Radar tổng hợp /100
📈 Smart Bands
Giá NEAR
EMA20
EMA50
EMA200
💰 Capital Flow
TVL 7D
TVL 30D
biểu đồ TVL
🌐 Network & Ecosystem
Validator
Staked NEAR
Active addresses
Transactions
DEX volume
MCap/TVL
🐋 Holder Flow / Investor Positioning
Tôi không bịa cost-basis/whale flow khi nguồn free chưa đủ dữ liệu.
Có sẵn chỗ để mở rộng NearBlocks V2.
🧭 Phân tích cuối
Giá 1D/7D
TVL 7D/30D
Network
Ecosystem
giải thích cách đọc.
Nguồn chính dùng CoinGecko, NEAR RPC và DeFiLlama; FastNear/NearBlocks có thể mở rộng thêm. FastNear hiện cung cấp các API account/staking/indexed views, còn NearBlocks free tier có giới hạn request nên tôi không để dashboard gọi liên tục gây phí/quota. 

Tôi đã test
Python compile: OK
AST parse toàn bộ file: OK
Smoke test riêng phần tính điểm NEAR: OK
Không phát hiện duplicate function ở module mới.
Tôi cũng sửa luôn lỗi tính 7D/30D trong phần prototype và làm fallback parse dữ liệu TVL linh hoạt hơn.
Lưu ý: môi trường test hiện tại không có Streamlit package và không cho container gọi Internet trực tiếp, nên tôi không giả vờ nói đã chạy live Streamlit/API. Phần code và logic đã được kiểm tra tĩnh + smoke test.

File đã tích hợp
Ông chỉ cần đổi file này thành app.py rồi chạy Streamlit.

Bản này tôi cố tình làm V1 gọn trước, chưa nhét mấy chỉ số kiểu “Diamond Price / All-in Cost / Break-even / Whale Accumulation” nếu chưa có dữ liệu đủ sạch. Bước tiếp theo hợp lý nhất là tôi làm V2 Holder Flow + top ví NEAR + staking flow + accumulation/distribution, lúc đó cái bảng sẽ bắt đầu giống A1 Radar nhưng số liệu có nguồn rõ ràng. 


app_NEAR_Monitor_V1.py
Code




def _near_rpc(method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": "near-monitor",
        "method": method,
        "params": params if params is not None else [],
    }
    r = requests.post(
        NEAR_RPC_URL,
        json=payload,
        headers={"content-type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    return j.get("result", {})


@st.cache_data(ttl=600, show_spinner=False)
def near_network_data():
    status = _near_rpc("status", [])
    validators = _near_rpc("validators", ["final"])
    current = validators.get("current_validators", []) or []
    total_stake_yocto = 0.0
    for v in current:
        total_stake_yocto += _near_num(v.get("stake"), 0.0)
    return {
        "chain_id": status.get("chain_id", "NEAR"),
        "latest_block": (status.get("sync_info") or {}).get("latest_block_height"),
        "validator_count": len(current),
        "total_stake_near": total_stake_yocto / 1e24,
        "validators": current,
    }


@st.cache_data(ttl=900, show_spinner=False)
def near_defillama_data():
    chain_url = "https://api.llama.fi/v2/chain/Near"
    hist_url = "https://api.llama.fi/v2/historicalChainTvl/Near"
    r1 = requests.get(chain_url, timeout=25)
    r1.raise_for_status()
    chain = r1.json()

    r2 = requests.get(hist_url, timeout=25)
    r2.raise_for_status()
    hist = r2.json()
    hist_rows = []
    for row in hist if isinstance(hist, list) else []:
        if isinstance(row, dict):
            ts = row.get("date")
            tvl = row.get("tvl")
            try:
                if isinstance(ts, (int, float)):
                    dt = pd.to_datetime(ts, unit="s", utc=True)
                else:
                    dt = pd.to_datetime(ts, utc=True)
                hist_rows.append({"date": dt, "tvl": float(tvl)})
            except Exception:
                pass
    hist_df = pd.DataFrame(hist_rows).sort_values("date") if hist_rows else pd.DataFrame(columns=["date", "tvl"])

    return {"chain": chain, "history": hist_df}


def _near_hist_change(hist_df, days):
    if hist_df is None or hist_df.empty:
        return np.nan
    end = hist_df.iloc[-1]["tvl"]
    cutoff = hist_df.iloc[-1]["date"] - pd.Timedelta(days=days)
    old = hist_df[hist_df["date"] <= cutoff]
    if old.empty:
        return np.nan
    base = float(old.iloc[-1]["tvl"])
    return (float(end) / base - 1.0) * 100.0 if base > 0 else np.nan


def _near_ema(series, span):
    return float(pd.Series(series).ewm(span=span, adjust=False).mean().iloc[-1]) if len(series) else np.nan


def _near_score_components(market, chart, defi, network):
    p = _near_num(market.get("price"))
    c24 = _near_num(market.get("change_24h"), 0.0)
    p7 = np.nan
    p30 = np.nan
    vol_ratio = np.nan
    if chart is not None and not chart.empty:
        s = chart.set_index("date")["price"].sort_index()
        now = float(s.iloc[-1])
        for d, name in [(7, "p7"), (30, "p30")]:
            old = s[s.index <= s.index[-1] - pd.Timedelta(days=d)]
            if not old.empty and old.iloc[-1] > 0:
                change = (now / float(old.iloc[-1]) - 1.0) * 100.0
                if name == "p7":
                    p7 = change
                else:
                    p30 = change
        vol = market.get("volume_24h")
        if vol and len(s) > 7:
            vol_ratio = float(vol) / max(float(s.pct_change().rolling(7).std().iloc[-1] or 0), 1e-9)

    hist = defi.get("history") if isinstance(defi, dict) else None
    tvl7 = _near_hist_change(hist, 7)
    tvl30 = _near_hist_change(hist, 30)

    # Transparent composite: no proprietary/AI claim.
    momentum = float(np.clip(50 + (0 if np.isnan(p7) else p7 * 2.0) + c24 * 1.0, 0, 100))
    ecosystem = float(np.clip(50 + (0 if np.isnan(tvl7) else tvl7 * 1.8) + (0 if np.isnan(tvl30) else tvl30 * 0.6), 0, 100))
    network = float(np.clip(50 + min(network.get("validator_count", 0), 500) * 0.04, 50, 75))
    risk = float(np.clip(50 + (0 if np.isnan(p30) else abs(p30) * 0.8) + (0 if np.isnan(c24) else max(c24, 0) * 0.4), 0, 100))
    flow = float(np.clip(50 + (0 if np.isnan(tvl7) else tvl7 * 2.2), 0, 100))
    total = float(np.clip(momentum * .30 + flow * .25 + network * .20 + ecosystem * .25, 0, 100))
    return {
        "momentum": momentum,
        "flow": flow,
        "network": network,
        "ecosystem": ecosystem,
        "risk": risk,
        "total": total,
        "p7": p7,
        "p30": p30,
        "tvl7": tvl7,
        "tvl30": tvl30,
        "ema20": _near_ema(chart["price"].to_numpy(), 20) if chart is not None and not chart.empty else np.nan,
        "ema50": _near_ema(chart["price"].to_numpy(), 50) if chart is not None and not chart.empty else np.nan,
        "ema200": _near_ema(chart["price"].to_numpy(), 200) if chart is not None and not chart.empty else np.nan,
    }


def _near_holder_flow():
    """Optional NearBlocks holder view. Chỉ bật nếu người dùng đã thêm API key."""
    if not NEARBLOCKS_API_KEY:
        return None
    url = "https://api.nearblocks.io/v1/stats"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {NEARBLOCKS_API_KEY}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def render_near_monitor():
    st.markdown("---")
    st.header("🟢 NEAR Monitor")
    st.caption(
        "V1 dùng nguồn dữ liệu công khai/có free tier: CoinGecko cho giá & market data, "
        "NEAR RPC cho trạng thái mạng/validator, DeFiLlama cho TVL hệ sinh thái. "
        "Các điểm Radar là công thức minh bạch của dashboard, không phải điểm AI từ bên thứ ba."
    )

    try:
        market = near_market_data()
        chart = near_market_chart(90)
        defi = near_defillama_data()
        network = near_network_data()
        score = _near_score_components(market, chart, defi, network)
    except Exception as e:
        st.warning(f"NEAR Monitor chưa lấy đủ dữ liệu: {e}")
        return

    p = market.get("price", np.nan)
    chain = defi.get("chain", {})
    hist = defi.get("history")
    tvl_now = _near_num(chain.get("tvl"))
    stables = _near_num(chain.get("stablecoinsMcap", chain.get("stablecoinsMcapUsd")))
    dex_vol = _near_num(chain.get("dexsVolume24h", chain.get("dexsVolume")))
    active = _near_num(chain.get("activeAddresses24h", chain.get("activeAddresses")))
    txs = _near_num(chain.get("transactions24h", chain.get("transactions")))

    # Market Snapshot
    st.subheader("📌 Market Snapshot")
    a, b, c, d, e, f = st.columns(6)
    a.metric("NEAR", f"${p:.4f}" if np.isfinite(p) else "N/A", f"{market.get('change_24h'):+.2f}%" if np.isfinite(_near_num(market.get('change_24h'))) else None)
    b.metric("Market Cap", _near_fmt_usd(market.get("market_cap")))
    c.metric("FDV", _near_fmt_usd(market.get("fdv")))
    d.metric("Volume 24h", _near_fmt_usd(market.get("volume_24h")))
    e.metric("Circulating", _near_fmt_near(market.get("circulating_supply")))
    f.metric("TVL NEAR", _near_fmt_usd(tvl_now))
    st.caption("Nguồn market: CoinGecko • TVL/stablecoin: DeFiLlama")

    # Radar
    st.subheader("🧠 NEAR Radar — điểm minh bạch")
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("Momentum", f"{score['momentum']:.0f}/100")
    r2.metric("Capital Flow", f"{score['flow']:.0f}/100")
    r3.metric("Network", f"{score['network']:.0f}/100")
    r4.metric("Ecosystem", f"{score['ecosystem']:.0f}/100")
