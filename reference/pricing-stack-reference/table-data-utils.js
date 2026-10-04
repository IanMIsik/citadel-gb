const _ = require('lodash');

const customRound = (number, precision = 0) =>
  number < 0
    ? -_.round(Math.abs(number), precision)
    : _.round(number, precision);

const showPriceSign = (price, sign) =>
  +price < 0 ? `-${sign}${Math.abs(price)}` : `${sign}${price}`;

function showUnitValues(
  dataArray,
  flagged,
  sp = '',
  sumOfDelta,
  pricingStackTableString,
  showSp,
  showBottomBorder
) {
  //sortBy the price of the items
  dataArray = _.sortBy(dataArray, ['price']);
  //reversing the array shows the largest prices first
  dataArray = dataArray.reverse();
  let collapseElement;
  let buttonClass;
  dataArray.forEach((element, index, array) => {
    const volDelta =
      element.vol_delta == null ? '' : customRound(element.vol_delta);
    const priceDelta =
      element.price_delta == null ? '' : customRound(element.price_delta, 1);
    const volToPrice =
      element.vol_to_price == null ? '' : customRound(element.vol_to_price);

    const price = customRound(element.price, 1);
    const vol = customRound(element.vol);

    //the key used to select the persisted classes from the storage
    let bmunitClass = `collapse${flagged}${sp}`;
    collapseElement = sessionStorage.getItem(bmunitClass)
      ? JSON.parse(sessionStorage.getItem(bmunitClass))['attributeClass']
      : `collapse${flagged}${sp} collapse show`;

    const buttonId = `${flagged}${sp}`;
    buttonClass = sessionStorage.getItem(bmunitClass)
      ? JSON.parse(sessionStorage.getItem(bmunitClass))['btnClass']
      : 'btn btn-secondary';
    let sumOfDeltaElement = `
    <td class=total data-sign=${sumOfDelta}>
      ${sumOfDelta}
    </td>`;
    if (index === 0) {
      // if the first element of the array
      pricingStackTableString += `
          <tr>
          <td>
            <span style=font-weight:bold;font-size:13px;>${showSp ? sp : ''}
            </span>
          </td>
            <td type=button class="${buttonClass}"
              style=font-weight:bold;
              data-class=${collapseElement}
              id=${buttonId}
              onclick=collapseToggle(${buttonId})
            >
              ${flagged}
            </td>
            <td> </td>
            <td> </td>
            <td> </td>
            <td> </td>
            <td> </td>
          </tr>
          <tr class=${
            array.length == 1 && showBottomBorder ? 'custom-border' : ''
          }>
            ${showSp ? sumOfDeltaElement : '<td></td>'}
            <td></td>
            <td class=pricing-stack-text>${element.bmunit}</td>
            <td
            data-sign=${element.vol}
            class=pricing-stack-text>${vol}</td>
            <td data-sign=${element.price}
            class=pricing-stack-text
            >
              ${showPriceSign(price, '£')}
            </td>
            <td
              data-sign=${priceDelta}
              class=pricing-stack-text
            >
              ${priceDelta}
            </td>
            <td
              data-sign=${volDelta}
              class=pricing-stack-text
            >
              ${volDelta}
            </td>
          <td data-sign=${volToPrice} class=pricing-stack-text>
              ${volToPrice}
            </td>
          </tr>
            `;
    } else {
      if (array.length - 1 == index) {
        //check if it's the last element
        pricingStackTableString += `
          <tr class=${
            !showSp ? 'custom-border' : showBottomBorder ? 'custom-border' : ''
          }>
            <td></td>
            <td></td>
            <td class=pricing-stack-text>${element.bmunit}</td>
            <td
              data-sign=${element.vol}
              class=pricing-stack-text
            >
              ${vol}
            </td>
            <td
            data-sign=${element.price}
            class=pricing-stack-text
            >
              ${showPriceSign(price, '£')}
            </td>
            <td
            data-sign=${priceDelta}
            class=pricing-stack-text
            >
              ${priceDelta}
            </td>
            <td
              data-sign=${volDelta}
              class=pricing-stack-text
            >
              ${volDelta}
            </td>
            <td
          <td data-sign=${volToPrice} class=pricing-stack-text>
              ${volToPrice}
            </td>
          </tr>
          `;
      } else {
        pricingStackTableString += `
          <tr class="${collapseElement}">
          <td></td>
          <td></td>
            <td class=pricing-stack-text>${element.bmunit}</td>
            <td data-sign=${element.vol} class=pricing-stack-text>
              ${vol}
            </td>
            <td data-sign=${element.price} class=pricing-stack-text>
              ${showPriceSign(price, '£')}
            </td>
            <td
              data-sign=${priceDelta}
              class=pricing-stack-text
            >
              ${priceDelta}
            </td>
            <td
              data-sign=${volDelta}
              class=pricing-stack-text
            >
              ${volDelta}
            </td>
            <td data-sign=${volToPrice} class=pricing-stack-text>
              ${volToPrice}
            </td>
          </tr>`;
      }
    }
  });
  return pricingStackTableString;
}
module.exports = { showUnitValues, customRound };
