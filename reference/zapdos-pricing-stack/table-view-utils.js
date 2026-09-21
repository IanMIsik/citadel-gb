const { showUnitValues, customRound } = require('./data-utils');
let pricingStackTableElement = document.getElementById('ps-table');
let genHtml = '';
let pricingStackTable = '';

function sumValues(array, dataAttribute) {
  const total = array.reduce((accumulator, currentValue) => {
    return accumulator + currentValue[dataAttribute];
  }, 0);
  return Number(total);
}
function generatePricingStackTable(data, spsArray) {
  genHtml = ''; //reset the string when new data is received
  let index = 0;
  for (const key in data) {
    if (data.hasOwnProperty(key)) {
      //use this index to show the latest sp from the sps array
      let latestSpString = spsArray[index];
      const dataBasedOnSp = data[latestSpString];
      if (dataBasedOnSp) {
        const flaggedFalseArray = dataBasedOnSp.filter(
          bmunit => bmunit['flagged'] === 'F'
        );
        const flaggedTrueArray = dataBasedOnSp.filter(
          bmunit => bmunit['flagged'] === 'T'
        );
        let showFsp = true;
        let showBottomBorder = false;
        let showTsp = flaggedFalseArray.length < 1 ? true : false;
        flaggedTrueArray.length == 0
          ? (showBottomBorder = true)
          : (showBottomBorder = false);
        showTsp ? (showBottomBorder = true) : '';
        const sumOfFalseDelta = sumValues(flaggedFalseArray, 'vol');
        const sumOfTrueDelta = sumValues(flaggedTrueArray, 'vol');
        let sumPerSpDelta = sumOfFalseDelta + sumOfTrueDelta;
        sumPerSpDelta = customRound(sumPerSpDelta);
        pricingStackTable = ``;
        pricingStackTable = showUnitValues(
          flaggedFalseArray,
          'F',
          latestSpString,
          sumPerSpDelta,
          pricingStackTable,
          showFsp,
          showBottomBorder
        );
        //shows the bottom border when the flagged F empty actions are more than 1
        flaggedFalseArray.length >= 0 && flaggedTrueArray.length >= 1
          ? (showBottomBorder = true)
          : (showBottomBorder = false);
        pricingStackTable = showUnitValues(
          flaggedTrueArray,
          'T',
          latestSpString,
          sumPerSpDelta,
          pricingStackTable,
          showTsp,
          showBottomBorder
        );
        //append the different sp table elements to the the generated sp html
        genHtml += pricingStackTable;
        //increment the index on every app run
        index++;
      }
    }
  }
  pricingStackTableElement.innerHTML = `
    <table class="table">
      <tr>
        <th class=pricing-stack-text>sp</th>
        <th class=pricing-stack-text>Flag</th>
        <th class=pricing-stack-text>Bmunit</th>
        <th class=pricing-stack-text>vol</th>
        <th class=pricing-stack-text>price</th>
        <th class=pricing-stack-text>price_d</th>
        <th class=pricing-stack-text>vol_d</th>
        <th class=pricing-stack-text>vol_to_price</th>
      </tr>
      ${genHtml}
    </table>`;
}

module.exports = generatePricingStackTable;
